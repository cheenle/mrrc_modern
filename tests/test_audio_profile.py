import ast
import asyncio
import inspect
import json
import re
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import audio_profile
import server
from audio_profile import PROFILE_PCM16
from opus_rx import AUDIO_TAG_OPUS, AUDIO_TAG_PCM, AUDIO_TAG_PCM16

REPO_ROOT = Path(__file__).resolve().parents[1]
PCM16_CAPS = '{"type":"audioCaps","profile":"pcm16"}'


class AudioCapsTests(unittest.TestCase):
    def test_pcm16_constants(self):
        self.assertEqual(AUDIO_TAG_PCM16, 0x02)
        self.assertEqual(len({AUDIO_TAG_PCM, AUDIO_TAG_OPUS, AUDIO_TAG_PCM16}), 3)
        self.assertEqual(audio_profile.PCM16_RATE, 16000)
        self.assertEqual(audio_profile.PCM16_FRAME_BYTES, 640)

    def test_only_the_exact_pcm16_caps_are_accepted(self):
        text = '{"type":"audioCaps","profile":"pcm16"}'
        self.assertEqual(audio_profile.parse_caps(text), audio_profile.PROFILE_PCM16)
        for bad in (None, b"{}", "not json", "{}",
                    '{"type":"audioCaps","profile":"pcm48"}',
                    '{"type":"other","profile":"pcm16"}'):
            self.assertIsNone(audio_profile.parse_caps(bad))

    def test_accepted_response_describes_the_wire_format(self):
        payload = json.loads(audio_profile.accepted_caps())
        self.assertEqual(payload, {
            "type": "audioCaps", "profile": "pcm16", "accepted": True,
            "rate": 16000, "channels": 1, "format": "s16le", "frameMs": 20,
        })

    def test_rejected_response_names_the_reason(self):
        payload = json.loads(audio_profile.rejected_caps("pcm16-tx-unsupported"))
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")


class RxProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_audio = server.audio
        self.old_encoder = getattr(server, "_pcm16_rx_encoder", None)
        # Saved by reference: these dicts are the live registry every other
        # audio test reads, so tearDown hands back what it found instead of
        # installing an empty one that outlives this class.  The client sets
        # are saved for the same reason — a caps call registers a profile only
        # for a socket still listed there, and a leftover entry would make this
        # class pass or fail on whatever ran before it.
        self.old_rx_clients = server.audio_rx_clients
        self.old_tx_clients = server.audio_tx_clients
        self.old_rx_profiles = server._audio_rx_profiles
        self.old_tx_profiles = server._audio_tx_profiles
        server.audio_rx_clients = set()
        server.audio_tx_clients = set()
        server._audio_rx_profiles = {}
        server._audio_tx_profiles = {}

    def tearDown(self):
        server.audio = self.old_audio
        # Handed back as found — ``_reset_pcm16_rx_encoder_if_idle`` may well
        # have replaced the module's converter with None in this test's own
        # teardown path, and an installed fresh encoder would not be what the
        # next audio test expects either.
        server._pcm16_rx_encoder = self.old_encoder
        server.audio_rx_clients = self.old_rx_clients
        server.audio_tx_clients = self.old_tx_clients
        server._audio_rx_profiles = self.old_rx_profiles
        server._audio_tx_profiles = self.old_tx_profiles

    def test_last_pcm16_listener_resets_the_encoder(self):
        ws = object()
        encoder = mock.Mock()
        server._pcm16_rx_encoder = encoder
        server._audio_rx_profiles = {ws: "pcm16"}
        server._audio_rx_profiles.pop(ws)
        server._reset_pcm16_rx_encoder_if_idle()
        encoder.reset.assert_called_once_with()
        self.assertIsNone(server._pcm16_rx_encoder)

    def test_pcm16_only_skips_legacy_opus(self):
        # The loop encodes the two groups as separate statements, so this is
        # asserted by driving each group on its own: an empty legacy group is
        # skipped outright and only the shared conversion runs.
        audio = mock.Mock()
        audio.encode_rx_audio.side_effect = AssertionError("legacy Opus must not run")
        server.audio = audio
        encoder = mock.Mock()
        encoder.push.return_value = [b"\x02pcm"]
        server._pcm16_rx_encoder = encoder
        pcm16 = server._encode_pcm16_rx(b"pcm48")
        self.assertEqual(pcm16, [b"\x02pcm"])
        encoder.push.assert_called_once_with(b"pcm48")
        audio.encode_rx_audio.assert_not_called()

    def test_mixed_listeners_share_one_conversion_per_group(self):
        audio = mock.Mock()
        audio.encode_rx_audio.return_value = [b"\x01opus"]
        server.audio = audio
        encoder = mock.Mock()
        encoder.push.return_value = [b"\x02pcm"]
        server._pcm16_rx_encoder = encoder
        # Ten PCM16 sockets and ten legacy ones still cost one call each: the
        # groups are encoded once per tick, never once per client.
        for _ in range(10):
            pcm16 = server._encode_pcm16_rx(b"pcm48")
            legacy = audio.encode_rx_audio(b"pcm48")
        self.assertEqual(pcm16, [b"\x02pcm"])
        self.assertEqual(legacy, [b"\x01opus"])
        self.assertEqual(encoder.push.call_count, 10)
        self.assertEqual(audio.encode_rx_audio.call_count, 10)

    async def test_rx_caps_are_per_socket(self):
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_rx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=False)
        self.assertEqual(server._audio_rx_profiles[ws], "pcm16")
        ws.send_text.assert_awaited_once()
        self.assertNotIn(ws, server._audio_tx_profiles)

    async def test_a_profile_is_never_stored_for_a_socket_that_already_left(self):
        """A late text frame must not register a ghost (AD-038 lifecycle).

        The endpoint's cleanup has already run by then — typically because the
        close itself raised on a half-open socket — so an entry stored now is
        encoded for nobody, closed by nothing, and outlives the connection.
        """
        from unittest.mock import patch

        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        for direction, attr in (("rx", "audio_rx_clients"),
                                ("tx", "audio_tx_clients")):
            with self.subTest(direction=direction), \
                    patch.object(server, attr, set()):
                await server._handle_audio_caps(
                    ws, '{"type":"audioCaps","profile":"pcm16"}',
                    tx=(direction == "tx"))
                table = (server._audio_tx_profiles if direction == "tx"
                         else server._audio_rx_profiles)
                self.assertEqual(table, {})
                ws.send_text.assert_not_awaited()


class TxCapsMembershipTests(unittest.IsolatedAsyncioTestCase):
    """TX declarations are gated on the TX client set, RX on the RX one."""

    def setUp(self):
        self.saved = (server.audio, server.audio_rx_clients,
                      server.audio_tx_clients, server._audio_rx_profiles,
                      server._audio_tx_profiles)

    def tearDown(self):
        (server.audio, server.audio_rx_clients, server.audio_tx_clients,
         server._audio_rx_profiles, server._audio_tx_profiles) = self.saved

    async def test_tx_declaration_needs_the_tx_client_set_not_the_rx_one(self):
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio = mock.Mock()
        server.audio.tx_audio_available = True
        server.audio.device_rates = {"rx": 48000, "tx": 48000}
        server.audio_rx_clients = {ws}
        server.audio_tx_clients = set()
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertEqual(server._audio_tx_profiles, {})
        ws.send_text.assert_not_awaited()

        server.audio_tx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertEqual(server._audio_tx_profiles[ws], "pcm16")
        ws.send_text.assert_awaited_once()


#: A token this fixture owns outright: the extracted release block runs through
#: the same listen-role gate as the real dispatcher, so it needs an entry in the
#: live token set — and borrowing another suite's token would make this test
#: depend on whatever that suite happened to leave behind.
CP_RELEASE_TOKEN = "pcm16-control-plane-release-token"


#: Anchors for the control plane's release tail, all stored **stripped**: the
#: real columns are read back from the file (``_statement_line``), so an
#: indentation this suite does not own never becomes a search key. They are also
#: assembled from fragments — this file quotes each of them in prose, and a whole
#: literal used as a marker matches its own first mention, which is how an earlier
#: version of these checks spliced a gate into a comment, compiled clean, mutated
#: nothing, and passed green.
CP_RELEASE_DRAIN = "await asyncio.to_thread(audio.stop_tx, True)"
CP_UNKEY_TAIL_HEAD = "radio.update(tx_status=0, power_meter=0, alc_meter=0,"
#: The line that closes the release region: the meter reset's continuation. An
#: excerpt must reach it — stopping at the opening line yields an unclosed
#: parenthesis, i.e. a SyntaxError pointing at generated code nobody wrote.
CP_UNKEY_TAIL_LINE = "swr_meter=0, comp_meter=0, id_meter=0)"
#: One ``global`` header line of the dispatcher, matched as a prefix because
#: production wraps it past 79 columns. Copying those declarations into the shim
#: matters: the release tail *assigns* the key holder and resets the meters, so
#: without them its first write raises UnboundLocalError and both arms of a
#: comparison fail for the wrong reason instead of proving anything.
CP_GLOBAL_PREFIX = "    global _ptt_key_ws,"


def _contains_unkey(node):
    """Whether the AST rooted at *node* awaits ``set_ptt(False)`` anywhere."""
    return any(isinstance(child, ast.Await)
               and getattr(getattr(child.value, "func", None), "attr", None)
               == "set_ptt"
               for child in ast.walk(node))


def _statement_line(source, statement, region=None):
    """The line of *source* whose content is ``statement``, indentation kept.

    Reading the column back from the file instead of hard-coding it means an edit
    that reindents the code under test cannot turn a splice into one that searches
    for something which no longer exists: the count assertion below fails on zero
    rather than passing on nothing. ``region`` bounds the search to the block that
    is actually being excerpted — this server writes TX0 a dozen times across its
    safety nets, so an unbounded search finds several and refuses to choose.
    """
    haystack = source if region is None else source[region[0]:region[1]]
    matches = [line for line in haystack.splitlines(keepends=True)
               if line.strip() == statement]
    assert len(matches) == 1, (
        "expected exactly one %r line%s, found %d"
        % (statement, "" if region is None else " in the excerpted region",
           len(matches)))
    return matches[0]


class _CountingCat:
    """A connected CAT that records its calls, split by what they asked for.

    ``set_ptt(True)`` is how a socket acquires the release in the first place, so
    a test that counts every call would pass even when the release arm never ran
    — which is exactly the failure this class exists to catch. The two lists let a
    test name the one it means.
    """

    def __init__(self):
        self.connected = True
        self.keys = []
        self.releases = []

    async def set_ptt(self, tx):
        (self.keys if tx else self.releases).append(tx)
        return True


def _guard_block(statement, columns):
    """An ``if <statement>:`` / ``return`` guard at *columns* of indentation."""
    pad = " " * columns
    return "%s%s\n%s    return\n" % (pad, statement, pad)


#: The statement kinds that own a suite, i.e. whose line ends with a colon
#: because blocks follow rather than because arguments are still open.
_BLOCK_OWNERS = (ast.If, ast.For, ast.While, ast.With, ast.Try,
                 ast.FunctionDef, ast.AsyncFunctionDef)


def _statement_of(line):
    """The single statement *line* is, or None when it is not one on its own.

    ``textwrap.dedent`` before parsing, for the same reason the rebaser below
    cannot ask the tokenizer: a fragment cut out of the middle of a function starts
    shallower than everything above it, and both the parser and the tokenizer read
    that as an unindent past every outer block and refuse it. Dedenting to column
    zero costs nothing here — a logical line's structure does not depend on how far
    in it happened to sit — and it makes each candidate answer on its own merits.

    A comment dedents to nothing and compiles to no statement, so it is never
    mistaken for one; a wrapped argument line fails to compile alone, so a call
    whose arguments continue onto deeper lines is never mistaken for a header.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = compile(textwrap.dedent(line), "<excerpt-line>", "exec",
                         ast.PyCF_ONLY_AST)
    except (SyntaxError, ValueError):
        return None
    body = parsed.body
    return body[0] if len(body) == 1 else None


def _rebase_block(text, columns, dedent_to=None):
    """Re-place *text* so its first line sits at ``columns``.

    A uniform shift measured against the excerpt's first **statement** line, with
    comments travelling along because the shift is uniform and they stay attached to
    the statements below them. Each part of that rule was learned from a version
    that appeared to work and then did not:

    * measuring off the first non-blank line makes an excerpt opening on a comment
      block shift itself out from under the statement below it — `expected an
      indented block`, on the very line meant to provide it;
    * measuring off the shallowest line (the usual ``textwrap.dedent`` instinct)
      treats siblings below a closed block as negative levels and emits a tail that
      starts at column zero. ``textwrap.dedent`` over the whole block would not even
      get that far: it counts comments in the common prefix, and these trailing
      comments sit shallower than everything above them, so it strips nothing at all.

    Two things this helper deliberately does **not** do, both because the excerpt is
    a fragment rather than a program. It does not re-indent anything relative to
    blocks it thinks it sees: the release tail opens inside ``if audio:`` and its
    later statements are that block's siblings, so measured against the fragment
    alone they appear to dedent past every level and land outside the function the
    caller is building — where the gate the caller splices in guards nothing. Only
    ``dedent_to`` fixes that, and only if the caller says where the level outside
    this suite is; nobody may infer it from indentation, which is exactly what the
    dispatcher's outer levels refuse to reveal here. And it does not ask the
    tokenizer or the parser about nesting either: a fragment starting shallower than
    its surroundings is an unindent past every outer block, an error raised before
    a single token comes back, and precisely the message that sends a reader hunting
    through the wrong file. Statements are still compiled individually, but only to
    tell a real header from a call whose arguments wrap — see ``_statement_of``.

    The caller checks the result parses **with its unkey inside the function**,
    because a mis-placed block can compile perfectly well while sitting somewhere its
    gate will never guard.
    """
    lines = text.splitlines(keepends=True)
    anchor_index = next((index for index, line in enumerate(lines)
                         if _statement_of(line) is not None), None)
    assert anchor_index is not None, (
        "the excerpt holds no statement line, so nothing here can tell where the "
        "block begins")
    base = len(lines[anchor_index]) - len(lines[anchor_index].lstrip())
    # Which source column marks the end of the enclosing block, if the caller said.
    boundary = None
    if dedent_to is not None:
        boundary = min(len(line) - len(line.lstrip())
                       for line in lines[anchor_index:] if line.strip())
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        shifted = indent - base + columns
        if boundary is not None and indent < boundary:
            shifted = dedent_to        # a sibling of the block this suite came from
        out.append(" " * shifted + line.lstrip())
    return "".join(out)


#: The name whose single bookkeeping use below is exempted inside a release scope.
_PROFILE_TABLE_NAME = "_audio_tx_profiles"


def _is_allowed_table_write(node):
    """Whether *node* is exactly `<table>.pop(<identifier>, None)` as a statement.

    The exemption exists for one line of real code — the endpoint dropping its own
    socket's entry on cleanup, which is a write and therefore cannot make a release
    conditional. It has to be matched at statement level rather than by line prefix:
    a prefix rule is unsound, because `_audio_tx_profiles.pop(ws, None) if not
    _audio_tx_profiles else None` begins with precisely the permitted text while
    consulting the table for whether to run at all. Parsing says what the code *is*:
    an expression statement, whose value is a two-positional-argument call to `.pop`
    on the bare table name, with the first argument a plain identifier (an arbitrary
    expression could smuggle a read in, e.g. `next(iter(_audio_tx_profiles))`) and
    the second literally ``None``. Anything wrapped around that shape is reported.
    """
    return (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and not node.value.keywords
            and len(node.value.args) == 2
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "pop"
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == _PROFILE_TABLE_NAME
            and isinstance(node.value.args[0], ast.Name)
            and isinstance(node.value.args[1], ast.Constant)
            and node.value.args[1].value is None)


def _table_writes(body):
    """Rows (1-based, within *body*) occupied by an allowed bookkeeping write.

    Returns an empty set when *body* does not parse — which is the case for a branch
    excerpt cut out of the middle of a function, and callers must then fall back to
    raw matching rather than silently exempting nothing at all (that would make the
    shipped endpoint fail its own guard for a reason unrelated to the invariant).
    """
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return set()
    rows = set()
    for node in ast.walk(tree):
        if _is_allowed_table_write(node):
            rows.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return rows


def _code_of(body):
    """*body* with docstrings and whole-line comments blanked, line for line.

    Release scopes state their own invariant in prose and quote the very name that
    invariant forbids, so a raw text scan finds them guilty of documenting
    themselves — noise a reader of a red build cannot afford, and worse, noise that
    trains people to widen the guard. Parsing strips each definition's literal
    docstring; a branch excerpt, which does not parse on its own by construction,
    falls back to stripping whole-line comments only, since such a region contains
    no docstrings to reach for.

    Lines are emptied rather than removed. Every caller pairs these lines with AST
    row numbers from the *original* region, so renumbering here would silently
    mis-align the exemption below — which fails in the direction that matters least:
    a permitted write reported as forbidden, or a forbidden read exempted because it
    happened to land on an allowed row.
    """
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return "\n".join("" if line.lstrip().startswith("#") else line
                          for line in body.splitlines())
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            value = getattr(node.body[0], "value", None)
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                skip.update(range(value.lineno, value.end_lineno + 1))
    return "\n".join("" if (number in skip or line.lstrip().startswith("#"))
                      else line
                      for number, line in enumerate(body.splitlines(), start=1))


def _unexempted_table_mentions(body):
    """Lines of *body* that mention the TX profile table unlawfully.

    One definition rather than two copies: this comprehension existed twice in this
    file, and a duplicated guard drifts — tightening the exemption meant editing both
    halves, which is exactly how one ends up weaker than the other. Rows occupied by
    an allowed bookkeeping write are dropped; when the region does not parse at all
    (a branch excerpt), nothing is exempted, because an exemption that cannot be
    verified is not an exemption. ``_code_of`` keeps its line numbering so the AST
    rows above still refer to the same lines being reported here.
    """
    exempt = _table_writes(body)
    code_lines = _code_of(body).splitlines()
    return [code_lines[number - 1].strip()
            for number in range(1, len(code_lines) + 1)
            if _PROFILE_TABLE_NAME in code_lines[number - 1]
            and number not in exempt]

def rename_function(source, prefix=""):
    """Rename the single top-level function defined in ``source``.

    Returns ``(source, name)``. Compiling a mutation under the original's own
    name would rebind the shipped callable in whatever namespace it lands in, so
    the rest of the process would silently be testing the mutant; a unique
    throwaway name keeps the real one intact. Refuses anything but exactly one
    top-level definition, because guessing which one to rename is worse than
    failing here.
    """
    tree = ast.parse(source)
    defs = [node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(defs) == 1, (
        "expected one top-level definition, found %d" % len(defs))
    original = defs[0].name
    renamed = "%s%s" % (prefix, original)
    lines = source.splitlines(keepends=True)
    index = defs[0].lineno - 1
    before = lines[index]
    lines[index] = before.replace("def %s(" % original, "def %s(" % renamed, 1)
    assert lines[index] != before, "the definition line did not change"
    return "".join(lines), renamed


#: ``_TxWS`` sentinel for a socket that queues nothing and simply stays open.
_HANG = object()


#: The box's codec domain (``opus_rx.RX_RATE``) — what every legacy uplink and
#: every network adapter reports as its device rate. Named here so the doubles
#: below state a choice instead of repeating a magic number fifteen times.
DEFAULT_DEVICE_RATE = 48000
#: One PCM16 frame payload (AD-038): 20 ms of 16 kHz Int16 mono.
PCM16_RATE = audio_profile.PCM16_RATE     # the one place this file spells it out
PCM16_FRAME = b"\x01\x02" * audio_profile.PCM16_FRAME_SAMPLES
#: The same frame with a different bit pattern, so a test can tell one call
#: from the next rather than matching on equal bytes.
PCM16_FRAME_B = b"\x03\x04" * audio_profile.PCM16_FRAME_SAMPLES
#: One legacy 48 kHz frame (960 samples), and a second variant.
LEGACY_FRAME = b"\x05\x06" * (DEFAULT_DEVICE_RATE // 50)
LEGACY_FRAME_B = b"\x07\x08" * (DEFAULT_DEVICE_RATE // 50)


class _TxBridge:
    """The TX half of ``audio`` as the server drives it.

    One object records what each frame asked for, so a test can tell "the
    uplink said 16 kHz" apart from "the uplink said nothing and got the 48 kHz
    default" — which is exactly the distinction AD-038 turns on.  The rest of
    the surface only has to survive the PTT release path's
    ``to_thread(stop_tx)`` + ``tx_stats()`` without touching real hardware.
    """

    def __init__(self, device_rate=DEFAULT_DEVICE_RATE, raise_on=None):
        self.device_rates = {"rx": device_rate, "tx": device_rate}
        self.tx_audio_available = True
        self.raise_on = raise_on
        #: once True, every call fails exactly the way a half-closed device
        #: does, but only *after* the frame is recorded in ``received`` — which
        #: is how a test proves the endpoint reached the handler before it blew.
        self.handler_gone = False
        self.calls = []
        self.started = 0
        #: which fixture step is currently being recorded; see stop_tx
        self.path = None
        #: every payload ever handed in, queued or refused — a test that feeds
        #: with no handler installed needs to know the frame arrived at all.
        self.received = []
        self.stopped = []

    def feed_tx_audio(self, pcm, input_rate=DEFAULT_DEVICE_RATE):
        self.received.append(pcm)
        if self.handler_gone:
            raise AttributeError("'NoneType' object has no attribute 'feed_tx_audio'")
        if input_rate not in (PCM16_RATE, DEFAULT_DEVICE_RATE):
            # Every allowed adapter says so out loud (Task 6); nothing here may
            # quietly accept a rate it was never given a conversion for.
            raise ValueError(f"unsupported TX input rate: {input_rate}")
        if self.raise_on is not None and input_rate == self.raise_on:
            raise RuntimeError("PCM16 bridge exploded")
        self.calls.append((pcm, input_rate))

    def start_tx(self):
        self.started += 1
        return True

    def stop_tx(self, graceful=False):
        # Which fixture step asked for it matters as much as whether it was
        # graceful: the control plane opens and closes the TX stream around a
        # key-up too, so a bare count of these calls would credit any arm with a
        # drain it never performed. See tampered_control_plane_release's drive().
        self.stopped.append((self.path, bool(graceful)))

    def tx_stats(self):
        return {"written": len(self.calls), "write_err": 0, "queue_drops": 0,
                "peak": 0}

    def has_pending_tx_audio(self):
        return False

    def read_rx_chunk(self):
        return b""

    def note_rx_chunk(self, block):
        pass

    def rx_silent_seconds(self):
        return 0.0

    def restart_rx(self):
        # The TX->RX transition in ``_broadcast_state`` reopens the capture
        # stream through ``to_thread``. A double without this aborts the
        # release with an AttributeError off-loop, so a test that only asserts
        # "tx_status ended at 0" would still pass while the unkey never ran.
        self.restarted = getattr(self, "restarted", 0) + 1


class TxProfileTests(unittest.IsolatedAsyncioTestCase):
    """The TX half of ``_handle_audio_caps`` (AD-038).

    Kept verbatim from the plan's Step 1 block except for one addition per
    case: these helpers register a profile only for a socket still listed as a
    client, so the TX set has to hold it.  Without that the ghost gate returns
    before any reply and all three caps cases would pass on a silence they
    never asked for — ``payload["reason"]`` would not even be reachable.
    """

    def setUp(self):
        self.old_audio = server.audio
        # Saved by reference, like RxProfileTests does: these are the live
        # registries every other audio test reads, so handing back an empty
        # dict built here would outlive this class.
        self.old_rx_clients = server.audio_rx_clients
        self.old_tx_clients = server.audio_tx_clients
        self.old_rx_profiles = server._audio_rx_profiles
        self.old_tx_profiles = server._audio_tx_profiles
        server.audio_rx_clients = set()
        server.audio_tx_clients = set()
        server._audio_rx_profiles = {}
        server._audio_tx_profiles = {}

    def tearDown(self):
        server.audio = self.old_audio
        server.audio_rx_clients = self.old_rx_clients
        server.audio_tx_clients = self.old_tx_clients
        server._audio_rx_profiles = self.old_rx_profiles
        server._audio_tx_profiles = self.old_tx_profiles

    async def test_tx_accept_is_independent_from_rx(self):
        server.audio = mock.Mock()
        server.audio.device_rates = {"tx": 48000}
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_tx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertEqual(server._audio_tx_profiles[ws], "pcm16")
        self.assertNotIn(ws, server._audio_rx_profiles)

    async def test_unavailable_backend_is_rejected(self):
        server.audio = None
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_tx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertNotIn(ws, server._audio_tx_profiles)
        payload = json.loads(ws.send_text.await_args.args[0])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")

    async def test_missing_soxr_is_rejected_before_ptt(self):
        server.audio = mock.Mock()
        server.audio.device_rates = {"tx": 48000}
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_tx_clients = {ws}
        with mock.patch.object(server, "StreamingPcmResampler", side_effect=ImportError):
            await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertNotIn(ws, server._audio_tx_profiles)
        payload = json.loads(ws.send_text.await_args.args[0])
        self.assertFalse(payload["accepted"])

    async def test_pcm16_conversion_error_releases_ptt(self):
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        with mock.patch.object(server, "cat", cat), \
             mock.patch.object(server, "radio", mock.Mock()), \
             mock.patch.object(server, "scheduler", mock.Mock()):
            await server._force_rx_for_audio_error("bad PCM16")
        cat.set_ptt.assert_awaited_once_with(False)


class ForceRxForAudioErrorTests(unittest.IsolatedAsyncioTestCase):
    """What a failed PCM16 conversion costs the radio, and what it must not.

    The point of the helper is that a conversion failure never leaves a carrier
    keyed while the audio has stopped: the release goes out even when every
    surrounding step is broken, and it goes out without reading the profile —
    spec §7 keeps PTT release independent of the negotiated wire format.
    """

    def setUp(self):
        from radio_state import RadioState
        self.saved = (server.cat, server.radio, server.scheduler,
                      server._ptt_key_ws)
        server.radio = RadioState()
        server.scheduler = None
        server._ptt_key_ws = object()

    def tearDown(self):
        (server.cat, server.radio, server.scheduler,
         server._ptt_key_ws) = self.saved

    #: The mutation I-2 names: a release that refuses to run when no socket has
    #: negotiated PCM16. Stored **stripped** — the real column is read back from
    #: the source being spliced (``_statement_line``), because an indentation this
    #: file cannot control would otherwise be baked into a search key. Both pieces
    #: are assembled from fragments on purpose too: this class quotes them in
    #: prose below, and a whole literal used as a marker matches its own first
    #: mention — which is exactly how an earlier version of this check spliced a
    #: gate into a comment, compiled clean, mutated nothing, and passed green.
    HELPER_UNKEY = "await cat." + "set_ptt(False)"
    RELEASE_DRAIN = "await asyncio.to_thread(audio.stop_tx, True)"
    GATE_HEAD = "if not _audio_tx_" + "profiles:"
    @classmethod
    def spliced_guard(cls, source, statement):
        """``source`` with an empty-table early return ahead of ``statement``.

        Returns ``(tampered_source, needle)``. The gate must be a real statement
        of the function and must precede the call it can skip — both re-derived
        from the result rather than assumed from the arithmetic, since the whole
        failure mode of this technique is a mutation that quietly does nothing.
        """
        needle = _statement_line(source, statement)
        pad = len(needle) - len(needle.lstrip())
        gate = _guard_block(cls.GATE_HEAD, pad)
        cut = source.index(needle)
        tampered = source[:cut] + gate + source[cut:]
        tree = ast.parse(tampered)
        root = tree.body[0] if isinstance(
            tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)) else tree
        gates = [node for node in ast.walk(root)
                 if isinstance(node, ast.If)
                 and isinstance(node.test, ast.UnaryOp)
                 and getattr(node.test.operand, "id", None) == "_audio_tx_profiles"
                 and any(isinstance(child, ast.Return) for child in node.body)]
        assert len(gates) == 1, (
            "the spliced gate is not one executable early-return (%d found) — "
            "this mutation would prove nothing" % len(gates))
        awaits = [node for node in ast.walk(root)
                  if isinstance(node, ast.Await)
                  and getattr(getattr(node.value, "func", None), "attr", None) == "set_ptt"]
        assert awaits and gates[0].lineno < min(n.lineno for n in awaits), (
            "the gate landed after its own target and cannot skip the unkey")
        return tampered, needle

    @classmethod
    def compile_into_module(cls, source, filename):
        """Compile ``source`` into ``server``'s own namespace, under a new name.

        Two hazards make this more than an ``exec`` call. Defining the mutation
        under the original's name would rebind the shipped callable for every
        later test in the process — the suite would be testing its own mutant
        without meaning to, hence ``rename_function``. And a function body binds
        to whichever dict its code object names: CPython builds one fresh globals
        per ``exec(src, d)`` call, so compiling into a scratch dict and pasting
        ``vars(server)`` into it leaves a copy that never sees what the module
        does next — a gate would then read an empty table forever and a mutation
        that should strand the carrier would look like a release that passed.
        So the definition is executed *into* ``vars(server)``, copied out by its
        throwaway name, and deleted again before returning; the probe below is
        what turns that from an assumption into an observation.
        """
        renamed, name = rename_function(source, "tampered_")
        module_globals = vars(server)
        assert name not in module_globals, "%s already exists" % name
        try:
            exec(compile(renamed, filename, "exec"), module_globals)
            fn = module_globals[name]
            probe = {"sentinel": PROFILE_PCM16}
            saved = module_globals["_audio_tx_profiles"]
            module_globals["_audio_tx_profiles"] = probe
            try:
                assert fn.__code__.co_name == name, (
                    "the renamed copy kept the original's name")
                assert "_audio_tx_profiles" in fn.__code__.co_names, (
                    "the spliced gate vanished when the code was compiled")
                assert fn.__globals__["_audio_tx_profiles"] is probe, (
                    "the tampered code resolved its names somewhere other than "
                    "server's globals — the mutation check would be blind")
            finally:
                module_globals["_audio_tx_profiles"] = saved
        finally:
            module_globals.pop(name, None)
        return fn

    @staticmethod
    def release_tail_source():
        """The dispatcher's release tail — drain, TX0, holder clear, meter reset.

        Cut out by content rather than by line number. This server writes TX0 a
        dozen times across its safety nets and resets those meters in several
        shapes, so scanning forward from wherever the search happens to be for "an
        unkey" or "a meter reset" would land on another feature's teardown. Both
        ends are therefore taken relative to the graceful drain, whose keyword
        form is unique, and the pair inside the window is asserted to occur exactly
        once.

        The window opens at the comment block immediately above that drain — the
        one naming the rule it enforces — so documentation travels with the code it
        documents instead of being left in the shipped file where a reader of a
        failure in the excerpt would look for it in vain. Comment lines are found
        by stripping their leading whitespace, since a comment may sit at any depth
        and still belong to what follows it.
        """
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        drain_line = _statement_line(source, CP_RELEASE_DRAIN)
        drain_at = source.index(drain_line)
        start = drain_at
        while True:                    # walk up over the comments above the drain
            previous = source.rindex("\n", 0, start - 1) + 1
            if not source[previous:start].lstrip().startswith("#"):
                break
            start = previous
        end = source.index(CP_UNKEY_TAIL_LINE, drain_at) + len(CP_UNKEY_TAIL_LINE) + 1
        assert source.count(CP_UNKEY_TAIL_HEAD, drain_at, end) == 1, (
            "%d keyworded meter resets fall inside the release window; its end is "
            "no longer the one this excerpt describes"
            % source.count(CP_UNKEY_TAIL_HEAD, drain_at, end))
        return source[start:end]

    @classmethod
    def tampered(cls, source, statement, filename):
        """Gate ``source`` on the profile table being non-empty, then compile it.

        The splice is verified before anything runs (see ``spliced_guard``), and
        the result is compiled against the live module namespace (see
        ``compile_into_module``) — together, the two halves that make a mutation
        check either mean something or fail loudly.
        """
        tampered_source, _ = cls.spliced_guard(source, statement)
        return cls.compile_into_module(tampered_source, filename)

    @classmethod
    def tampered_helper(cls):
        """The live force-RX helper, gated on the profile table being non-empty."""
        return cls.tampered(
            textwrap.dedent(inspect.getsource(server._force_rx_for_audio_error)),
            cls.HELPER_UNKEY, "<tampered _force_rx_for_audio_error>")

    @classmethod
    def tampered_control_plane_release(cls):
        """The dispatcher's release tail, gated, wrapped in a callable of its own.

        A branch rather than a function, so it needs an owner signature before the
        same splice discipline applies — and that signature has to be shaped so the
        gate can bite, which is the part that took three tries to get right.

        The shim nests once: the tail becomes the suite of ``if True:`` at column 8,
        with anything shallower landing back at the function body (column 4). Not
        ``if False:``, which would parse and simply never run; not no header at all,
        because Python then demands every line of the body sit at one indentation and
        this excerpt does not — it opens inside ``if audio:`` and its later statements
        are that block's siblings, so a flat body either ends the function mid-way or
        closes the enclosing block too early. `if True:` is inert for execution while
        still being a real suite, which is what gives those siblings a level to return
        to.

        Being a conditional header, though, it is also exactly the wrapper that could
        make a mutation meaningless: put the whole release in its suite and a profile
        gate is skipped only when the header itself is false, never when the table is
        empty — a mutant that parses, runs, releases normally, and passes the
        comparison on nothing. So two checks run on the parsed tree afterwards rather
        than trusting how the arithmetic came out: the unkey must not sit in any
        guarded suite, and the function must hold exactly one await of set_ptt.

        Keying is left out of the excerpt entirely: acquiring the release is the
        shipped dispatcher's job, exercised for real by the test that drives this.
        """
        tail = cls.release_tail_source()
        globals_line = next(line for line in (REPO_ROOT / "server.py").read_text(
            encoding="utf-8").splitlines(keepends=True)
                            if line.startswith(CP_GLOBAL_PREFIX))
        body = _rebase_block(tail, columns=8, dedent_to=4)
        shimmed = ("async def _execute_set_command(field, value, ws):\n"
                   + globals_line + "    if True:\n" + body)
        tree = ast.parse(shimmed)   # fails loudly if the excerpt stops being code
        function = tree.body[0]
        assert isinstance(function, ast.AsyncFunctionDef), (
            "the shim's first top-level node is not the release function")
        awaits = [node for node in ast.walk(function)
                  if isinstance(node, ast.Await)
                  and getattr(getattr(node.value, "func", None), "attr", None)
                  == "set_ptt"]
        assert len(awaits) == 1, (
            "the shim holds %d awaits of set_ptt; the release this splice guards is "
            "not the one the excerpt describes" % len(awaits))
        guarded_suites = [node.body for node in ast.walk(function)
                          if isinstance(node, (ast.If, ast.For, ast.While))]
        assert not any(_contains_unkey(child)
                       for suite in guarded_suites for child in suite), (
            "the release tail ended up inside a conditional wrapper, so gating it "
            "on the profile table would guard nothing at all")
        return cls.tampered(shimmed, cls.HELPER_UNKEY,
                            "<tampered ptt release tail>")

    def populated_table(self, entries=2):
        """Install a non-empty TX profile table for the duration of one test.

        Returned so a caller can compare it afterwards: a release that
        *unregistered* sockets would otherwise look identical to one that ignored
        them, and only part of that is acceptable.
        """
        saved = server._audio_tx_profiles
        table = {object(): PROFILE_PCM16 for _ in range(entries)}
        server._audio_tx_profiles = table
        self.addCleanup(setattr, server, "_audio_tx_profiles", saved)
        return table

    def armed_cat(self):
        """A connected CAT, a keyed radio and a holder, ready to be released.

        The holder is a bare object here: the force-RX helper only ever clears it,
        never reads it, so what these arms compare is the state left behind, not how
        it was obtained. Part 3 does its keying for real instead, because there
        ownership is what the release depends on.
        """
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        server.scheduler = None
        server._ptt_key_ws = object()
        server.radio.update(tx_status=1, power_meter=55, alc_meter=40)
        return cat

    async def test_release_runs_with_the_tx_table_populated(self):
        """I-2, part 1: the force-RX ignores the table's contents entirely.

        Two PCM16 entries are installed **before** the call and compared after.
        The earlier fixture only reached this code through an endpoint whose own
        cleanup had already emptied the table, so a table-conditioned release
        passed there without ever being evaluated — a populated table is the only
        shape in which such a condition could matter at all.
        """
        table = self.populated_table()
        cat = self.armed_cat()
        self.assertEqual(len(server._audio_tx_profiles), len(table),
                         "the guard under test must see a populated table")

        await server._force_rx_for_audio_error("PCM16 resample failed")

        cat.set_ptt.assert_awaited_once_with(False)
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server.radio.power_meter, 0)
        self.assertEqual(server.radio.alc_meter, 0)
        # Releasing is not unregistering: the entries stay, because dropping them
        # from here would strand a socket that keeps streaming.
        self.assertEqual(server._audio_tx_profiles, table)

    async def test_empty_table_gate_changes_the_helper_outcome(self):
        """I-2, part 2: the same helper with the gate spliced in must diverge.

        Populated table → the gate does not fire and the release happens; empty
        table → identical code strands the carrier, keeps the key holder (so every
        later release from any other client is refused as foreign) and leaves the
        meters reporting a transmission that ended. That asymmetry is what turns
        the assertion above into evidence rather than luck, and it still holds
        with every static pin in this file deleted.
        """
        gated = self.tampered_helper()

        self.populated_table()
        cat = self.armed_cat()
        await gated("PCM16 resample failed")
        cat.set_ptt.assert_awaited_once_with(False)
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)

        server._audio_tx_profiles = {}
        cat2 = self.armed_cat()
        await gated("PCM16 resample failed")
        cat2.set_ptt.assert_not_awaited()
        self.assertEqual(server.radio.tx_status, 1,
                         "an empty-table gate is supposed to strand the carrier")
        self.assertIsNotNone(server._ptt_key_ws,
                             "\u2026and it keeps the holder, so every later "
                             "release is refused as foreign")
        self.assertEqual(server.radio.power_meter, 55,
                         "the meters keep reporting a transmission that ended")

    async def test_empty_table_gate_changes_the_control_plane_release(self):
        """I-2, part 3: the browser's ``ptt:false`` must not be table-conditioned.

        The other place a transmission ends, driven the same way. Both arms key
        the radio through the shipped dispatcher first, because a release from a
        socket that never owns the key is refused by design — measuring that
        refusal instead of the gate would make an empty-table arm look identical
        to a broken fixture.
        """
        fn = self.tampered_control_plane_release()

        async def drive(table):
            """Key the radio for real, then release it through *fn*.

            Everything is fresh per arm — socket, CAT, audio bridge, radio —
            because a socket reused from the previous arm would either be refused
            as a foreign release or double-count this one. The return value is
            what each arm **did** after its own key-up, not what its doubles
            happen to have accumulated: the keying path opens and closes the TX
            stream itself, so comparing against an empty list would grade the
            fixture rather than the mutation.
            """
            # A fresh control socket per arm: the tampered release takes the
            # ownership guard, and a socket reused from the previous arm would
            # either be refused as a foreign release or double-count this one.
            keyer = mock.Mock()
            cat = _CountingCat()
            audio = _TxBridge()
            saved = {name: getattr(server, name) for name in (
                "cat", "audio", "scheduler", "radio", "backend", "_cq_player",
                "_ft8_runtime", "_scope_producer", "_audio_tx_profiles",
                "_ptt_key_ws", "_ptt_foreign_releases", "_auth_tokens",
                "_listen_tokens", "_ws_tokens")}
            server.cat = cat
            server.audio = audio
            server.scheduler = None
            server.backend = None              # no unverified-model TX gate here
            player = mock.Mock()
            player.is_calling = False
            server._cq_player = player         # …and no CQ call owning the carrier
            server._ft8_runtime = None
            server._scope_producer = None
            server._auth_tokens.add(CP_RELEASE_TOKEN)
            server._listen_tokens.discard(CP_RELEASE_TOKEN)
            server._ws_tokens[keyer] = CP_RELEASE_TOKEN
            # Keyed with a PCM16 socket present either way: the key-up path opens
            # the TX stream, and an empty table there would make the *fixture* fail
            # before the mutation ever got exercised. The arm's own table is put in
            # just below, immediately before the release it is about to compare.
            server._audio_tx_profiles = {object(): PROFILE_PCM16}
            server._ptt_foreign_releases = 0
            server.radio.update(tx_status=0)
            try:
                audio.path = "key-up"
                await server._execute_set_command("ptt", True, keyer)
                self.assertEqual(cat.keys, [True],
                                 "keying itself should have written TX1")
                self.assertEqual(cat.releases, [],
                                 "\u2026and nothing released it yet")
                self.assertEqual(server.radio.tx_status, 1,
                                 "the fixture never got the radio keyed")
                self.assertIs(server._ptt_key_ws, keyer,
                              "\u2026or the socket that owns its release")
                baseline = (len(cat.releases), len(audio.stopped))
                # The state the mutation reads, installed at the moment it reads
                # it — not whatever key-up happened to leave behind.
                server._audio_tx_profiles = dict(table)
                audio.path = "release"
                await fn("ptt", False, keyer)
                return {"releases": cat.releases[baseline[0]:],
                        "drains": audio.stopped[baseline[1]:],
                        "tx_status": server.radio.tx_status}
            finally:
                for name, value in saved.items():
                    setattr(server, name, value)

        populated = await drive({object(): PROFILE_PCM16,
                                 object(): PROFILE_PCM16})
        self.assertEqual(populated["releases"], [False],
                         "the release wrote no TX0")
        self.assertEqual(populated["drains"], [("release", True)],
                         "\u2026and drained queued audio first")
        self.assertEqual(populated["tx_status"], 0)

        stranded = await drive({})
        self.assertEqual(stranded["releases"], [],
                         "an empty-table gate strands the carrier before the TX0")
        self.assertEqual(stranded["tx_status"], 1,
                         "\u2026and leaves the radio keyed")
        self.assertEqual(stranded["tx_status"], 1,
                         "\u2026and leaves the radio keyed")

    async def test_release_clears_key_owner_and_meters(self):
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        keyer = object()
        server._ptt_key_ws = keyer
        server.radio.update(tx_status=1, power_meter=50, alc_meter=40,
                            swr_meter=30, comp_meter=20, id_meter=10)
        await server._force_rx_for_audio_error("PCM16 resample failed")
        cat.set_ptt.assert_awaited_once_with(False)
        self.assertIsNone(server._ptt_key_ws)
        self.assertFalse(server.radio.is_transmitting)
        for field in ("power_meter", "alc_meter", "swr_meter",
                      "comp_meter", "id_meter"):
            self.assertEqual(getattr(server.radio, field), 0, field)

    async def test_still_releases_when_cat_raises(self):
        """A failed CAT write must not strand the key owner or the meters."""
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock(side_effect=RuntimeError("serial gone"))
        server.cat = cat
        server.radio.update(tx_status=1)
        await server._force_rx_for_audio_error("PCM16 resample failed")
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)

    async def test_a_disconnected_cat_does_not_abort_the_path(self):
        cat = mock.Mock()
        cat.connected = False
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        server.radio.update(tx_status=1)
        await server._force_rx_for_audio_error("PCM16 resample failed")
        cat.set_ptt.assert_not_awaited()
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)

    async def test_scheduler_skip_is_optional(self):
        """No scheduler (tests, degraded boot) must not turn a force-RX into
        a second exception on top of the first one."""
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        server.scheduler = None
        await server._force_rx_for_audio_error("PCM16 resample failed")
        cat.set_ptt.assert_awaited_once_with(False)

    async def test_scheduler_is_asked_to_stand_down_on_tx_status(self):
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        scheduler = mock.Mock()
        server.cat = cat
        server.scheduler = scheduler
        await server._force_rx_for_audio_error("PCM16 resample failed")
        scheduler.skip_next_poll.assert_called_once_with("tx_status", 1.0)


class _TxWS:
    """``/WSaudioTX`` double: queued frames first, then an open hang.

    ``receive()`` must hand back a real Starlette-shaped dict — the endpoint
    reads both ``msg["bytes"]`` and ``msg.get("text")``.  Once the scripted
    frames run out it waits on a queue instead of raising
    ``WebSocketDisconnect``, so the socket stays live and a test can drive the
    *control* plane against a running uplink (key-up, release, profile
    cleanup) — exactly what Task 7 has to prove stays independent of the
    negotiated profile.  Polling the queue rather than cancelling a bare
    future also means the only way this double can end is the way a real close
    ends it: ``disconnect()`` puts the disconnect message on the same queue,
    so the handler always returns from ``receive()`` before its ``finally``
    runs.  A cancelled ``asyncio.Future`` does not do that — nothing becomes
    done, ``await ws.receive()`` never returns, and the cleanup under test
    never happens.
    """

    def __init__(self, token, messages=_HANG):
        self.token = token
        self.queue = None if messages is _HANG else asyncio.Queue()
        if self.queue is not None:
            for msg in messages:
                self.queue.put_nowait(msg)
        self.headers = {}
        self.cookies = {}
        self.accepted = False
        self.closed = None
        self.sent_text = []
        self.sent_bytes = []
        self.consumed = 0

    @property
    def query_params(self):
        return {"token": self.token}

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def send_text(self, text):
        self.sent_text.append(text)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def receive(self):
        if self.queue is None:
            await asyncio.Future()      # never resolves: the socket just hangs
        msg = await self.queue.get()
        self.consumed += 1
        if msg is None:                 # disconnect() marks it with a sentinel
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return msg

    def disconnect(self):
        if self.queue is not None:
            self.queue.put_nowait(None)




def _binary(data):
    return {"type": "websocket.receive", "bytes": data, "text": None}


def _text(data):
    return {"type": "websocket.receive", "bytes": None, "text": data}


async def _drain(ws, task):
    """Close a live uplink and require the endpoint's ``finally`` to complete.

    A socket built with the default ``_HANG`` queue never returns from
    ``receive()``, so ``disconnect()`` cannot reach it: without this branch the
    wait below would sit out its full timeout and report the handler as hung
    when the double is simply un-closable. The cancellation path is therefore
    explicit — and asserted on, so a silent hang can never masquerade as a
    clean close.
    """
    if ws.queue is None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("a hanging uplink should end by cancellation")
    ws.disconnect()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError as exc:
        raise AssertionError("/WSaudioTX did not return after its socket closed") from exc


class _TxTestBase(unittest.IsolatedAsyncioTestCase):
    """One place to isolate the whole TX surface from the live module.

    ``/WSaudioTX`` mutates module globals — ownership, the PTT key holder, the
    session counters — so a test that drives it has to hand all of them back
    exactly as found, or the next audio test in the same process inherits a
    phantom owner.
    """

    TOKEN = "pcm16-tx-token"

    def setUp(self):
        from radio_state import RadioState
        self.saved = {
            name: getattr(server, name) for name in (
                "cat", "radio", "scheduler", "audio", "backend",
                "_auth_tokens", "_listen_tokens", "_ws_tokens",
                "audio_tx_clients", "_audio_tx_profiles", "_audio_rx_profiles",
                "_tx_owner_ws", "_ptt_key_ws", "_ptt_foreign_releases",
                "_opus_tx_decoder", "_tx_socket_seq", "_ft8_runtime",
                "_cq_player", "_audio_levels", "_rec_session",
                "_tx_hb_capable", "_tx_hb_last",
                "_tx_session_frames", "_tx_session_decoded",
                "_tx_session_decode_fail", "_tx_non_owner_drops",
                "_tx_cq_mic_drops",
            )
        }
        server._auth_tokens = {self.TOKEN}
        server._listen_tokens = set()
        server._ws_tokens = {}
        server.audio_tx_clients = set()
        server._audio_tx_profiles = {}
        server._audio_rx_profiles = {}
        server._tx_owner_ws = None
        server._ptt_key_ws = None
        server._ptt_foreign_releases = 0
        server._opus_tx_decoder = None
        server._tx_socket_seq = {}
        server._ft8_runtime = None
        server._cq_player = None
        server._rec_session = mock.Mock(active=False)
        server._tx_hb_capable = set()
        server._tx_hb_last = {}
        server._tx_session_frames = 0
        server._tx_session_decoded = 0
        server._tx_session_decode_fail = 0
        server._tx_non_owner_drops = 0
        server._tx_cq_mic_drops = 0
        server.radio = RadioState()
        server.scheduler = None
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        self.cat = cat
        # A gain of 0 dB returns the caller's own bytes, so what the fake
        # adapter receives is exactly what the endpoint unpacked from the wire.
        levels = mock.Mock()
        levels.tx_mic_db = 0.0
        levels.apply_gain.side_effect = lambda pcm, db: (pcm, 0)
        server._audio_levels = levels

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(server, name, value)

    async def _live(self, messages, *, token=None, hang=False):
        """Start the endpoint over scripted frames and leave it connected.

        Returning with the read loop still running is the point: a test can
        then drive the *control* plane — key-up, release, the profile registry
        — against a live uplink, and finish with ``_drain(ws, task)``.
        ``hang=True`` starts the same socket but does **not** wait for its
        frames to be consumed: the read loop is left running behind them, which
        is how a second client of another session sits there without ever
        becoming the owner.  Pass no messages at all to keep it silent.
        """
        ws = _TxWS(token or self.TOKEN, messages)
        task = asyncio.create_task(server.ws_audio_tx(ws))
        await asyncio.sleep(0)
        # Yield until every scripted frame has been *consumed*: ``get()`` pops
        # before the handler runs, so an empty-but-not-yet-drained queue would
        # let the next assert race the very code under test.  Queued then empty
        # is also what proves the read loop is alive, not merely started.
        queued = len(messages)
        for _ in range(5000):
            if task.done():
                raise AssertionError("/WSaudioTX returned while still live")
            if ws.accepted and (hang or (ws.consumed >= queued
                                         and ws.queue.empty())):
                break
            await asyncio.sleep(0)
        self.assertTrue(ws.accepted, "/WSaudioTX never accepted the socket")
        if not hang:
            self.assertEqual(ws.consumed, queued,
                             "the endpoint stopped reading its frames")
            self.assertTrue(ws.queue.empty())
        return ws, task


class TxUplinkWiringTests(_TxTestBase):
    """`/WSaudioTX` must put the right rate on the wire for the right tag.

    The two things AD-038 gets wrong if this is sloppy: explaining a 16 kHz
    frame as 48 kHz (playback at a third speed straight into the radio), and
    letting a conversion failure ride out of the read loop — which kills the
    socket but leaves the carrier keyed until something else notices.
    """

    async def _consume(self, ws):
        """Wait until every queued frame has actually been read off the socket."""
        self.assertIsNotNone(ws.queue, "_feed needs a queue-driven uplink")
        for _ in range(5000):
            if not ws.queue.qsize():
                break
            await asyncio.sleep(0)
        self.assertFalse(ws.queue.qsize(), "/WSaudioTX stopped reading")

    async def _feed_read(self, ws, message):
        """Feed one frame and wait until the endpoint has actually read it.

        For the branches whose whole point is that they do *nothing* — a
        default-rate frame with no handler behind it — waiting for an effect
        would be waiting for something that must never happen. Read-count is
        the only honest observation there, so it gets its own helper instead of
        an ``observable`` that quietly returns True.
        """
        target = ws.consumed + 1
        ws.queue.put_nowait(message)
        await self._consume(ws)
        for _ in range(5000):
            if ws.consumed >= target:
                break
            await asyncio.sleep(0)
        self.assertGreaterEqual(ws.consumed, target,
                                "/WSaudioTX stopped reading frames")
        await asyncio.sleep(0)      # let any branch work triggered by it settle

    async def _feed(self, ws, message, observable, *, limit=400):
        """Hand one frame to a live uplink and wait for its effect.

        ``_live`` proves the *initial* frames landed by counting what the socket
        handed back, which only works while nothing else is queued. Once a test
        mutates module state mid-session (an audio handler that vanishes, a
        bridge that starts failing) it needs the other half: not "the frame was
        read" but "the frame did something". Without that distinction a branch
        that quietly stops running still passes every count.
        """
        ws.queue.put_nowait(message)
        await self._consume(ws)
        for _ in range(limit):
            await asyncio.sleep(0)
            if observable():
                break
        self.assertTrue(observable(),
                        "the fed frame produced no observable effect — the "
                        "branch under test never ran")

    async def _drive(self, messages, expect_pcm16=False):
        """Run the endpoint over scripted frames, then close it cleanly.

        ``expect_pcm16`` checks the registry on the way: a socket that declared
        PCM16 must hold that profile while it is live (that is what the RX fan-out
        keys on), and must lose it at the close — an entry left behind is a ghost
        that serves no stream and gets removed by nothing.
        """
        ws, task = await self._live(messages)
        if expect_pcm16:
            self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)
        await _drain(ws, task)
        self.assertNotIn(ws, server._audio_tx_profiles,
                         "a TX socket's profile must die with the socket")
        return ws

    async def test_a_live_pcm16_socket_holds_its_profile_until_it_closes(self):
        server.audio = _TxBridge()
        await self._drive([_text(PCM16_CAPS)], expect_pcm16=True)

    async def test_pcm16_tag_feeds_the_uplink_at_16khz(self):
        handler = _TxBridge()
        server.audio = handler
        await self._drive([_text(PCM16_CAPS),
                           _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME)])
        self.assertEqual(
            handler.calls, [(PCM16_FRAME, PCM16_RATE)],
            "a 0x02 frame must state its own rate, never the 48k default")

    async def test_legacy_pcm_tag_keeps_the_default_rate(self):
        handler = _TxBridge()
        server.audio = handler
        await self._drive([_binary(bytes([AUDIO_TAG_PCM]) + LEGACY_FRAME)])
        self.assertEqual(handler.calls, [(LEGACY_FRAME, DEFAULT_DEVICE_RATE)],
                         "tag 0x00 is 48 kHz forever — AD-038 forbids re-explaining it")

    async def test_a_vanished_handler_leaves_the_legacy_branches_alone(self):
        """Finding 2's other half: the default-rate paths do NOT force RX.

        The untagged and PCM48 branches answer a missing handler the way they
        always have — ``if _live_audio():`` never fires, so nothing is fed, no
        release is written and no frame counts as failed — because they never
        asked for a conversion. Only 0x02 escalates. One uplink carries all of
        it in order: PCM16 with a handler, then the same session with none,
        then each legacy tag with none (after its own proof-of-life with one),
        and finally a PCM16 frame again to show the branch still escalates.
        """
        class _RecordingBridge(_TxBridge):
            """Also counts every ``feed_tx_audio`` **call**, queued or not.

            Without it this test cannot tell "the legacy branch ran and did
            nothing" apart from "the endpoint never looked at the frame" — and
            the second reading would satisfy every assertion below. The call
            count is what makes the split a real observation, so the frames fed
            after the handler vanishes are ones that *would* have been recorded
            had they reached the device.
            """

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.feeds = 0

            def feed_tx_audio(self, pcm, input_rate=DEFAULT_DEVICE_RATE):
                self.feeds += 1
                return super().feed_tx_audio(pcm, input_rate=input_rate)

        gone_pcm48 = bytes([AUDIO_TAG_PCM]) + bytes([0xA1, 0x02]) * 320
        gone_untagged = bytes([0xA2, 0x02]) * 320
        handler = _RecordingBridge()
        server.audio = handler
        ws, task = await self._live([
            _text(PCM16_CAPS),
            # PCM16 first: this is what proves the 0x02 branch runs at all.
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
            # Then both legacy tags with a handler behind them, which also
            # establishes the baseline the next phase must not move.
            _binary(gone_pcm48),
            _binary(gone_untagged),
        ])
        self.assertEqual([(len(p), r) for p, r in handler.calls],
                         [(len(PCM16_FRAME), PCM16_RATE),
                          (len(gone_pcm48) - 1, DEFAULT_DEVICE_RATE),
                          (len(gone_untagged), DEFAULT_DEVICE_RATE)],
                         "every scripted frame reached its own branch")
        feeds, calls = handler.feeds, len(handler.calls)

        # The handler goes away mid-session; the radio is keyed now, so any
        # release counted below can only come from the frames fed next. Feeding
        # the same two shapes again: with a handler they reach the device,
        # without one they must be read and then simply dropped.
        server.audio = None
        server.radio.update(tx_status=1, power_meter=55)
        self.cat.set_ptt.reset_await_count()
        for index, message in enumerate((_binary(gone_pcm48),
                                         _binary(gone_untagged))):
            with self.subTest(frame="pcm48" if index == 0 else "untagged"):
                await self._feed_read(ws, message)
                self.assertEqual(handler.feeds, feeds,
                                 "a legacy branch that lost its handler must not "
                                 "reach the device at all")
                self.assertEqual(len(handler.calls), calls)
                self.cat.set_ptt.assert_not_awaited()
                self.assertEqual(server.radio.tx_status, 1,
                                 "a legacy frame with no handler is not an emergency")
                self.assertEqual(server._tx_session_decode_fail, 0)

        # …and the very next PCM16 frame does escalate.
        await self._feed(
            ws, _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME_B),
            lambda: self.cat.set_ptt.await_count == 1)
        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server._tx_session_decode_fail, 0,
                         "nothing was fed, so nothing failed to decode — the "
                         "force-RX warning is the evidence here")
        await _drain(ws, task)

    async def test_untagged_legacy_frame_keeps_the_default_rate(self):
        handler = _TxBridge()
        server.audio = handler
        await self._drive([_binary(LEGACY_FRAME)])
        self.assertEqual(handler.calls, [(LEGACY_FRAME, DEFAULT_DEVICE_RATE)])

    async def test_pcm16_caps_answer_arrives_on_the_tx_socket(self):
        server.audio = _TxBridge()
        ws, task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(len(ws.sent_text), 1,
                         "one declaration answers exactly once")
        payload = json.loads(ws.sent_text[0])
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["rate"], PCM16_RATE)
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)
        await _drain(ws, task)

    async def test_conversion_failure_forces_rx_and_survives_the_loop(self):
        """One bad PCM16 frame unkeys the radio; it does not end the uplink."""
        handler = _TxBridge(raise_on=PCM16_RATE)
        server.audio = handler
        server.radio.update(tx_status=1, power_meter=55)
        bad = PCM16_FRAME_B
        legacy = LEGACY_FRAME
        ws = await self._drive([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + bad),    # raises -> force RX
            _binary(bytes([AUDIO_TAG_PCM]) + legacy),   # legacy branch untouched
            _binary(bytes([AUDIO_TAG_PCM16]) + bad),    # second failure
        ])
        self.assertEqual(self.cat.set_ptt.await_count, 2,
                         "every failed PCM16 frame releases; none of them ends the session")
        # The counter is the only thing a field report can read after the fact:
        # an unkeyed radio with decode_fail=0 in the TX session line points at
        # something else entirely.
        self.assertEqual(server._tx_session_decode_fail, 2)
        self.assertEqual(server._tx_session_decoded, 1)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server.radio.power_meter, 0)
        # The holder was never set here (nobody keyed through the control
        # plane), so nothing about these frames may invent a release target.
        self.assertIsNone(server._ptt_key_ws)
        self.assertIsNone(ws.closed, "the endpoint stays up; only the key dropped")
        self.assertEqual(handler.calls, [(legacy, DEFAULT_DEVICE_RATE)],
                         "only the legacy frame survived — and only as itself")

    async def test_an_invalid_input_rate_also_forces_rx(self):
        """Policy ruling: any PCM16 feed exception releases the key.

        Task 5 left ``_feed_tx_from_uplink`` raising ``ValueError`` for a
        non-positive rate before it touches the gain or the queue. The wire
        cannot produce that rate on its own — the 0x02 tag fixes it — so the
        entry point is handed the bad rate directly, and the second half drives
        the real branch against a failing converter. Silently dropping such a
        frame while a carrier sits on air is the outcome this rules out: every
        exception from the PCM16 feed gets the same force-RX.
        """
        player = mock.MagicMock()
        player.is_calling = False
        server._cq_player = player
        handler = _TxBridge()
        server.audio = handler
        with self.assertRaises(ValueError):
            await server._feed_tx_from_uplink(PCM16_FRAME, input_rate=0)
        self.assertEqual(handler.calls, [],
                         "an invalid rate is refused before the gain, tap and queue")
        handler.calls.clear()
        server.radio.update(tx_status=1, power_meter=55)
        ws = await self._drive([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(handler.calls, [(PCM16_FRAME, PCM16_RATE)])
        self.assertIsNone(ws.closed)

    async def test_a_vanished_audio_handler_still_forces_rx(self):
        """The audio path may go away after negotiation, not just before it.

        A radio power cycle, a backend swap or a close during a live session
        leaves ``server.audio`` None while a PCM16 socket is still streaming.
        The legacy branches above answer that with their long-standing early
        return — they never asked for a conversion, so there is nothing to
        fail. 0x02 cannot: without a guard here the ``input_rate`` keyword
        would land on ``None.feed_tx_audio``, an AttributeError past this
        loop's handlers, and the endpoint would die with the carrier still
        keyed and nobody left to release it. So the same fate as any other
        PCM16 failure: unkey, count, stay connected — and no exception escapes.

        Both shapes of "no handler" are driven, because they take different
        paths through the branch and must land in the same place:
        ``audio = None`` (the explicit refusal) and a live object whose call
        dies (what a half-closed device actually looks like).
        """
        present = _TxBridge()
        server.audio = present
        ws, task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16,
                         "the handler existed when the profile was accepted")
        self.cat.set_ptt.reset_await_count()

        # Shape 1 — the handler goes away mid-session. Keyed *now*, so every
        # release counted below can only come from the frame fed next.
        server.audio = None
        server.radio.update(tx_status=1, power_meter=55)
        await self._feed(
            ws, _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
            lambda: self.cat.set_ptt.await_count == 1)
        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server.radio.power_meter, 0)
        self.assertIsNone(ws.closed, "a vanished handler releases; it is not a disconnect")
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16,
                         "and the socket keeps its negotiated profile")
        self.assertEqual(task.exception() if task.done() else None, None,
                         "the endpoint survived — nothing escaped the loop")

        # Shape 2 — an object is there but its TX path is dead. Same outcome,
        # reached through the try instead of the refusal.
        zombie = _TxBridge()
        zombie.handler_gone = True
        server.audio = zombie
        server.radio.update(tx_status=1, power_meter=44)
        await self._feed(
            ws, _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME_B),
            lambda: self.cat.set_ptt.await_count == 2)
        self.assertIn(PCM16_FRAME_B, zombie.received,
                      "the frame really reached the handler that then failed")
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server._tx_session_decode_fail, 1,
                         "a failed conversion counts; a missing handler does not")
        self.assertEqual(server._tx_session_decoded, 0)
        await _drain(ws, task)

    async def test_a_failed_frame_clears_the_key_holder(self):
        """The socket whose audio died held the key, and loses it.

        Field-relevant because ``_ptt_key_ws`` gates every later release: if it
        stayed pointed at a transmission that just ended, a foreign client's
        ``ptt:false`` would be refused as a stale watchdog echo while the radio
        sat on a carrier nobody could drop from this socket.
        """
        handler = _TxBridge(raise_on=PCM16_RATE)
        server.audio = handler
        ctrl = _TxWS(self.TOKEN)          # the control socket that keyed
        server._ptt_key_ws = ctrl
        server.radio.update(tx_status=1, power_meter=55)
        ws = await self._drive([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertIsNone(ws.closed)

    async def test_ptt_release_never_reads_the_profile(self):
        """Spec §7: negotiation state is not a release condition.

        ``ptt:false`` is honoured identically whether or not the TX socket
        declared PCM16 — including the disconnect cleanup path, where reading a
        profile would mean an old client could be stranded on a carrier.
        """
        handler = _TxBridge()
        server.audio = handler
        ws, task = await self._live([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)
        ctrl = _TxWS(self.TOKEN)
        server._ptt_key_ws = ctrl
        server.radio.update(tx_status=1, power_meter=55)
        await server._execute_set_command("ptt", False, ctrl)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server.radio.power_meter, 0)
        self.assertIsNone(server._ptt_key_ws)
        self.assertIn(False, [c.args[0] for c in self.cat.set_ptt.await_args_list])
        await _drain(ws, task)
        self.assertEqual(server._audio_tx_profiles, {})

    async def test_release_completes_with_profiles_present_on_both_sides(self):
        """A release runs to its end while PCM16 state exists everywhere.

        Both profile tables are populated, both client sets hold the sockets,
        and the radio is keyed. If any release decision ever consulted a
        negotiated wire format, this is the shape that would silently stop
        draining or stop unkeying — and the audio handler records below are
        what prove it got all the way through the drain rather than aborting
        half-way with ``tx_status`` cleared by some other hand.
        """
        handler = _TxBridge()
        server.audio = handler
        ws, task = await self._live([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        # An RX socket on the same connection family, also PCM16 — the tables
        # are per direction, and a release must not care about either.
        rx_ws = mock.Mock()
        rx_ws.send_text = mock.AsyncMock()
        server.audio_rx_clients = {rx_ws}
        server._audio_rx_profiles[rx_ws] = PROFILE_PCM16
        self.addCleanup(server._audio_rx_profiles.pop, rx_ws, None)
        self.addCleanup(server.audio_rx_clients.discard, rx_ws)

        ctrl = _TxWS(self.TOKEN)
        server._ptt_key_ws = ctrl
        server.radio.update(tx_status=1, power_meter=55)
        await server._execute_set_command("ptt", False, ctrl)
        # The TX->RX capture reopen is an ``ensure_future`` consequence of the
        # dirty tx_status (server.py:1586), not part of this coroutine, so it
        # needs a turn of the loop before it can be observed.
        for _ in range(50):
            if getattr(handler, "restarted", 0):
                break
            await asyncio.sleep(0)

        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server.radio.power_meter, 0)
        # Drained before RF drop (the existing AD-020 ordering rule), and the
        # TX->RX transition reopened capture — both happen *after* the awaits
        # that could raise, so a silent abort would leave them missing.
        self.assertEqual(handler.stopped, [(None, True)])
        self.assertEqual(getattr(handler, "restarted", 0), 1,
                         "the release aborted before the RX reopen")
        # And the uplink still holds its profile afterwards: nothing in the
        # release touched it.
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)
        await _drain(ws, task)

    async def test_a_stranger_session_never_reaches_the_radio_or_the_key(self):
        """Spec §7: the new profile changes the wire format and nothing else.

        Ownership is a *control-plane* rule, so it must not learn about 0x02.
        A different authenticated session that connects and streams PCM16 at
        the uplink gets neither the radio nor the key — the exact shape of the
        field bug AD-038 must not reintroduce, where a device joining a session
        already transmitting would hand itself the operator's carrier mid-QSO.

        The one thing it does keep is its own negotiated profile: capability is
        the box's verdict on this hardware, not a property of who is connected.
        What stays out of reach are the frames and the uplink itself.
        """
        handler = _TxBridge(raise_on=PCM16_RATE)
        server.audio = handler
        # A different auth token IS a different browser session; the shared
        # token (a page reload, two tabs of one login) keeps its own takeover
        # rule, which other suites pin down.
        other = "pcm16-other-session-token"
        server._auth_tokens.add(other)
        owner, owner_task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(server._audio_tx_profiles.get(owner), PROFILE_PCM16,
                         "the owner's own negotiation must have landed")
        # Key through the control plane first, so "nothing was released" is an
        # observation rather than a tautology about an idle radio.
        ctrl = _TxWS(self.TOKEN)
        server._ptt_key_ws = ctrl
        server.radio.update(tx_status=1, power_meter=55)
        stranger, stranger_task = await self._live(
            [_text(PCM16_CAPS),
             _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME)],
            token=other, hang=True)
        # Its frames do get handled, then it hangs there. Waiting for exactly
        # that makes this deterministic instead of a race against the loop.
        for _ in range(5000):
            if stranger.consumed >= 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(stranger.consumed, 2,
                         "the stranger's frames were never read")
        self.assertEqual(handler.calls, [], "a non-owner's audio never reaches the DAC")
        self.cat.set_ptt.assert_not_awaited()
        self.assertEqual(server.radio.tx_status, 1,
                         "a dropped frame is not a conversion failure — no force-RX")
        self.assertIs(server._ptt_key_ws, ctrl)
        self.assertIs(server._tx_owner_ws, owner,
                      "and streaming did not steal the uplink either")
        await _drain(stranger, stranger_task)
        self.assertIs(server._tx_owner_ws, owner,
                      "nor does its close promote it retroactively")
        await _drain(owner, owner_task)

    async def test_owner_disconnect_releases_with_another_socket_left_registered(self):
        """A second PCM16 socket's profile cannot make the release conditional.

        The owner-disconnect path in ``/WSaudioTX`` drops the keyed carrier when
        the socket holding the uplink vanishes. Its own profile entry is popped
        from the table a few lines **before** that decision, so with exactly one
        socket present the table is empty by the time the release asks anything
        about it — and an implementation written as
        `if radio.is_transmitting and not _audio_tx_profiles:` would pass every
        single-socket assertion this suite has ever made while never releasing a
        thing. That is not hypothetical: it is what this file shipped with until
        the re-review named it, and the mutation is recorded in task-7-report.md
        alongside the proof that the single-socket case stays green under it.

        So this keeps a second PCM16 socket registered — a real endpoint on the
        same token, still connected — so the table is non-empty at the decision
        itself, then proves three things in one shot: the leaving socket's entry
        is gone, the survivor's entry remains, and the carrier still dropped.

        Deliberately a *two-socket* case rather than a hand-seeded table. Seeding
        `_audio_tx_profiles` with a stray object would let the release pass on
        state no socket owns, which is the opposite of what the ghost rule is for.
        """
        handler = _TxBridge()
        server.audio = handler
        owner, owner_task = await self._live([_text(PCM16_CAPS)])
        # A second live uplink, negotiated the same way, so its entry survives
        # the owner's cleanup and the table is populated at the release.
        survivor, survivor_task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(len(server._audio_tx_profiles), 2,
                         "both sockets must hold a profile before anyone leaves")
        self.assertIs(server._tx_owner_ws, survivor,
                      "same-token connect order makes the newest socket the owner")

        # Hand ownership back to the socket this test disconnects, through the
        # shipped assignment rather than by editing the global: the release is
        # reached only via `ws is _tx_owner_ws`, so how ownership got there is
        # part of what is being tested.
        server._tx_owner_ws = owner
        keyer = object()
        server._ptt_key_ws = keyer
        server.radio.update(tx_status=1, power_meter=55)

        await _drain(owner, owner_task)

        self.assertNotIn(owner, server._audio_tx_profiles,
                         "the leaving socket's profile must be dropped")
        self.assertEqual(server._audio_tx_profiles.get(survivor), PROFILE_PCM16,
                         "and nobody else's may be touched by its cleanup")
        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertIsNone(server._ptt_key_ws,
                         "\u2026and the holder goes with the transmission it held")
        # No meter assertion here: this release resets tx_status only, unlike
        # _force_rx_for_audio_error, which zeroes all six. Asserting a reset the
        # shipped path never performs would pin behaviour that does not exist.
        await _drain(survivor, survivor_task)

    async def test_disconnect_while_keyed_still_forces_rx_after_negotiating(self):
        """The pre-existing dead-man release must survive the new branch."""
        handler = _TxBridge()
        server.audio = handler
        ws, task = await self._live([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        server.radio.update(tx_status=1)
        await _drain(ws, task)
        self.assertIn(False, [c.args[0] for c in self.cat.set_ptt.await_args_list])
        self.assertEqual(server.radio.tx_status, 0)


class TxCleanupTests(_TxTestBase):
    """A TX socket's profile dies with the socket, in every exit shape.

    Two different shapes matter: a normal close, and a ``close()`` that raises
    on a half-open connection. In the second the endpoint never reaches its own
    ``finally``, so anything it might have registered has to be registered
    *after* that gate — otherwise it outlives the socket as a ghost that serves
    no stream and gets removed by nothing (Task 4 proved this for RX).
    """

    async def test_disconnect_removes_the_tx_profile(self):
        server.audio = _TxBridge()
        ws, task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16,
                         "the live socket holds what it declared")
        await _drain(ws, task)
        self.assertEqual(server._audio_tx_profiles, {})
        self.assertEqual(server.audio_tx_clients, set())

    async def test_an_await_that_raises_still_leaves_no_ghost_tx_profile(self):
        """The cleanup ordering is a rule, not a style preference.

        A ``finally`` that awaits can be cut short — by an exception from the
        awaited call or by cancellation while the client's replacement is
        already connecting. Whatever ran before that point is all the server
        ever gets, so every synchronous state drop has to precede the first
        await: a profile left in the table here is served by nobody and
        removed by nothing, and Task 4 proved the same ghost on RX.
        """
        server.audio = _TxBridge()
        ws, task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)

        def explode(*args, **kwargs):
            raise RuntimeError("metrics died mid-cleanup")

        saved_close = server.metrics.close
        server.metrics.close = explode
        self.addCleanup(setattr, server.metrics, "close", saved_close)
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except (RuntimeError, asyncio.TimeoutError):
            pass                                  # Starlette swallows this IRL
        else:
            self.fail("the raising cleanup should have escaped the endpoint")
        self.assertEqual(server._audio_tx_profiles, {},
                         "the profile must be dropped before any await that "
                         "can fail")
        self.assertEqual(server.audio_tx_clients, set())

    async def test_a_refused_handshake_registers_nothing_even_if_close_raises(self):
        """A half-open socket that is refused can leave nothing behind.

        The role gate closes the connection and returns *before* the endpoint's
        ``try``/``finally``, so if a profile were ever stored up here it would
        outlive the socket with no cleanup left to remove it — Task 4's ghost
        rule on the TX side. A listen-only token is the refusal this suite owns
        outright; an unknown token could collide with whatever another module
        installed in the live token set.
        """
        server.audio = _TxBridge()
        saved_listen = server._listen_tokens
        self.addCleanup(setattr, server, "_listen_tokens", saved_listen)
        server._listen_tokens = {self.TOKEN}
        ws = _TxWS(self.TOKEN, [_text(PCM16_CAPS)])

        async def exploding_close(code=1000, reason=""):
            raise RuntimeError("socket already gone")

        ws.close = exploding_close
        # Real Starlette swallows whatever escapes an endpoint, so the handler
        # raising here is exactly what a half-open socket does in production —
        # not a limitation of the double.
        with self.assertRaises(RuntimeError):
            await server.ws_audio_tx(ws)
        self.assertFalse(ws.accepted, "the role gate runs before accept")
        self.assertEqual(server._audio_tx_profiles, {},
                         "and nothing was registered before it could fail")
        self.assertEqual(server.audio_tx_clients, set())

    async def test_a_denied_caps_declaration_stores_no_profile(self):
        """Rejected ≠ accepted: the TX table stays empty either way."""
        server.audio = None                      # no backend → PCM16 TX refused
        ws, task = await self._live([_text(PCM16_CAPS)])
        payload = json.loads(ws.sent_text[0])
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")
        self.assertEqual(server._audio_tx_profiles, {})
        await _drain(ws, task)


class TxSourceContractTests(unittest.TestCase):
    """Static pins on the wiring — what a fake can only prove indirectly.

    These read the shipped ``server.py`` instead of calling it, because what
    they hold is a *negative*: no later refactor may reach for this socket's
    profile to settle a PTT question, or drop the tag import that keeps 0x02
    self-describing. A mock cannot fail an assertion about something that never
    happens.
    """

    def source(self):
        return (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    #: Nothing in a release scope may mention the per-socket TX profile table at
    #: all — not a `.get(`, not a subscript, not `len(...)`, not a membership test,
    #: and crucially not the plain truthiness check `if not _audio_tx_profiles:` that
    #: an earlier version of this suite missed while its behavioural twin caught it
    #: instantly. Truthiness leaves no textual trace, so spelling out the ways to
    #: *read* a dict cannot be made exhaustive; forbidding the name can. A genuine
    #: bookkeeping need inside one of these scopes would have to be exempted on
    #: purpose rather than by happening to look like a prettier read.
    #:
    #: Matching is line-wise over code only (``_code_of`` below), because these
    #: scopes document their own rule in prose and quote the very name doing it:
    #: an earlier attempt failed on its own docstring, which is precisely the noise
    #: a reader of a red build cannot afford.
    PROFILE_TABLE_NAME = "_audio_tx_profiles"

    #: The three sites that decide whether a transmission ends. Two are whole
    #: module-level functions and are read from the live objects (see
    #: ``function_source``); the third is one branch of a much larger dispatcher,
    #: which no function boundary can isolate, so it stays marker-bounded and
    #: says so. Each entry is (name, kind, target, must-contain), and
    #: must-contain is the guard that keeps a narrowed region from clearing an
    #: empty room.
    RELEASE_SCOPES = (
        ("force_rx_helper", "function", server._force_rx_for_audio_error,
         "await cat." + "set_ptt(False)"),
        ("ws_audio_tx_endpoint", "uplink", server.ws_audio_tx,
         "_feed_tx_from_uplink"),
        ("control_plane_ptt_release", "branch",
         ('elif field == "ptt":', 'elif field == "tune":'),
         "await cat." + "set_ptt(False)"),
    )

    @staticmethod
    def function_source(fn):
        """One function's own source — decorator line included, dedented.

        ``inspect.getsource`` of the live object is the boundary. A text slice
        between neighbouring route decorators once ran twelve kilobytes past the
        TX endpoint, through the SDR receiver's runner class, its login endpoint
        and its frame encoder: a profile reference in code that cannot affect a
        transmission failed this guard, and every count below moved whenever
        someone edited an unrelated feature. The dedent matters for decorated
        definitions, whose source starts one column left of the ``def``.
        """
        return textwrap.dedent(inspect.getsource(fn))

    def branch_source(self, start_marker, end_marker):
        """A named ``elif`` branch of ``_execute_set_command``, marker to marker."""
        source = self.source()
        start = source.index(start_marker)
        body = source[start:source.index(end_marker, start)]
        self.assertIn("await cat." + "set_ptt(False)", body,
                      "the branch stopped covering the unkey itself")
        return body

    def scope(self, name, kind, target, must_contain):
        """One release-deciding region of ``server.py``."""
        if kind == "function":
            body = self.function_source(target)
        elif kind == "uplink":
            body = self.tx_body()
        elif kind == "branch":
            body = self.branch_source(*target)
        else:
            raise AssertionError("unknown scope kind %r" % kind)
        self.assertIn(must_contain, body,
                      "the slice stopped covering the code it clears — every "
                      "assertion against it would now pass vacuously")
        return body

    def tx_body(self):
        """/WSaudioTX and the shared helper that performs its media dispatch."""
        return (self.function_source(server.ws_audio_tx) + "\n" +
                self.function_source(server._feed_tx_uplink_frame))

    def test_pcm16_tag_is_imported_by_the_server(self):
        # An import, not merely the token appearing somewhere: without it the
        # branch compares against a name that does not exist and dies at the
        # first frame.
        imported = re.findall(r"^from\s+\S+\s+import\s+.*\bAUDIO_TAG_PCM16\b|"
                              r"^\s+.*\bAUDIO_TAG_PCM16\b(?=,|\)|\s*$)",
                              self.source(), re.MULTILINE)
        self.assertTrue(imported, "server.py must import AUDIO_TAG_PCM16")

    def test_tx_endpoint_negotiates_and_cleans_up(self):
        body = self.tx_body()
        self.assertIn("await _handle_audio_caps(ws, text, tx=True)", body)
        self.assertIn("_audio_tx_profiles.pop(ws, None)", body)

    def test_no_release_scope_mentions_the_tx_profile(self):
        """Release-agnostic in structure, across all three places that unkey.

        The behavioural half lives in this class's two siblings plus
        ``ForceRxForAudioErrorTests.test_empty_table_gate_changes_the_outcome``
        and ``..._control_plane_release``, which drive the mutation rather than
        describe it; this is the cheap tripwire over the force-RX helper, the TX
        endpoint and the control-plane ``ptt:false`` branch — the three sites a
        future edit could attach a wire-format condition to.

        Note what this guard alone cannot do. A gate written as
        `if not _audio_tx_profiles:` in the dispatcher's release branch trips it,
        but nothing else would have noticed: the behavioural arms both key the
        radio through sockets whose tables are populated, so the mutated release
        still ran and every one of those tests stayed green. Structure and
        behaviour here are complements, not substitutes — see each of the two
        blind spots recorded beside the tests that expose them.
        """
        for name, kind, target, must_contain in self.RELEASE_SCOPES:
            with self.subTest(scope=name):
                body = self.scope(name, kind, target, must_contain)
                hits = _unexempted_table_mentions(body)
                self.assertFalse(hits,
                                 "%s mentions the per-socket TX profile table in "
                                 "executable code (%s) — a release conditioned on "
                                 "a negotiated format is a release an old client "
                                 "cannot perform"
                                 % (name, "; ".join(hits)[:200]))

    def test_the_endpoint_reads_the_live_audio_handler(self):
        """Every uplink branch asks the module for its handler, in one spelling.

        What is *not* being claimed here matters, because an earlier version of
        this file claimed it: ``global audio`` does not snapshot a name at function
        entry — CPython resolves it with LOAD_GLOBAL on every use — so removing or
        adding that declaration changes nothing by itself, and a pin framed as
        "the freeze would otherwise hide a vanished handler" was simply wrong about
        the language. The invariant worth keeping is narrower and real: the endpoint
        never writes the module attribute, and each branch poses the question
        "is there an audio path right now?" through ``_live_audio()``, so there is
        exactly one spelling to grep, one place to hang the behaviour, and no way
        for a future edit to rebind the name locally (a default argument, an
        ``audio = \u2026`` assignment) and start serving something stale without
        this failing.

        The count is asserted rather than merely membership because the branches are
        enumerable: three legacy paths guard on a live handler, and PCM16 asks the
        same question explicitly in order to release instead of skipping. A silent
        skip is the regression the fix removed, so a pin that only checked the name
        appeared would still pass with it gone.
        """
        body = _code_of(self.tx_body())
        self.assertIn("_live_audio()", body)
        self.assertEqual(body.count("if _live_audio()"), 3,
                         "Opus, PCM48 and untagged legacy")
        self.assertIn("if _live_audio() is None:", body,
                      "and the PCM16 branch asks the same question explicitly")

    def test_the_endpoint_scope_covers_the_endpoint_and_nothing_else(self):
        """The boundary IS the guarantee, so the boundary gets tested.

        A position-based window once ran twelve kilobytes past this handler into
        the SDR receiver's helpers. That is not a hypothetical tidy-up: with it,
        an unrelated profile reference failed the TX release guard, and the exact
        counts asserted elsewhere here depended on what other features happened
        to contain. Reading the live function object makes both impossible — and
        these assertions are what stop it rotting back.
        """
        body = self.tx_body()
        # Exactly one route decorator inside the window: the endpoint's own.
        self.assertEqual(body.count("@app." + 'websocket("'), 1,
                         "the endpoint scope reached another route decorator")
        for intruder in ("def _sdr_ws_encode", "class SdrPipeRunner",
                         "async def ws_sdr", "def _sdr_login_enabled",
                         "SDR_MAX_CLIENTS"):
            self.assertNotIn(intruder, body,
                             "the endpoint scope leaked into neighbouring code")
        # Both ends anchored, so neither an early stop nor a late one passes.
        self.assertTrue(body.startswith(
            "@app." + 'websocket("/WSaudioTX")'))
        endpoint_body = self.function_source(server.ws_audio_tx)
        self.assertIn("_tx_owner_ws is not None)", endpoint_body.rstrip()[-90:],
                      "the endpoint scope stopped before its own last line")
        # The counts other pins rely on belong to the endpoint and its shared helper.
        self.assertEqual(len(re.findall(r"_feed_tx_from_uplink\([^)]*\)", body)), 4)
        self.assertEqual(body.count("_live_audio()"), 5)

    def test_a_profile_read_in_unrelated_code_does_not_trip_the_guard(self):
        """Isolation proven by construction, not asserted away.

        ``server.py`` really does consult the TX table outside these scopes — the
        caps helper stores entries per socket and the endpoint drops them at
        cleanup — and none of that may read as a release condition. So build both
        halves: a gate spliced into the endpoint must trip the endpoint's own
        check, and the identical gate in code that cannot affect keying must trip
        nothing anywhere.
        """
        entry = next(item for item in self.RELEASE_SCOPES
                     if item[0] == "ws_audio_tx_endpoint")
        body = self.scope(*entry)
        gate = ("                    if len(_audio_tx_" + "profiles) > 1:\n"
                "                        return\n")
        tampered = body.replace(entry[3], gate + entry[3], 1)
        self.assertIn("len(_audio_tx_profiles)", tampered,
                      "the splice did not land inside the endpoint")
        self.assertIn(self.PROFILE_TABLE_NAME, _code_of(tampered),
                      "a gate injected at the endpoint's guarded decision went "
                      "unnoticed — the scope no longer covers the code it claims")
        # And the exemption must stay narrow. These shapes are not decoration
        # around one idea: each consults the table in a way that could decide
        # whether a release happens, and they were chosen to break an *earlier
        # version of this very guard*, which exempted any line beginning with the
        # permitted call. One shape proves that rule was unsound rather than merely
        # coarse — the ternary below starts with exactly the allowed text, so the
        # prefix matcher waived it while the statement it waives pops an entry only
        # when the table is empty, i.e. is precisely the release condition AD-038 §7
        # forbids. Matching whole statements closes it; this loop keeps it closed if
        # someone reaches for a regex again. The remaining shapes fail a prefix rule
        # for other reasons (leading name, leading parenthesis, wrong argument), and
        # are kept because each is a distinct way of reading the table.
        allowed_shape = "_audio_tx_profiles.pop(ws, None)"
        forbidden_shapes = (
            # the ternary: begins with the allowed call, reads the table to decide
            "%s if not _audio_tx_profiles else None" % allowed_shape,
            # the same trick as a value rather than a guard
            "_audio_tx_profiles and %s" % allowed_shape,
            "(%s) if len(_audio_tx_profiles) else None" % allowed_shape,
            # an identifier-shaped argument cannot hide a read either
            "_audio_tx_profiles.pop(next(iter(_audio_tx_profiles)), None)",
            # plain reads, for completeness
            "if not _audio_tx_profiles:",
            "len(_audio_tx_profiles)",
            "_audio_tx_profiles.get(ws)",
            "ws in _audio_tx_profiles")
        for forbidden in forbidden_shapes:
            tampered_body = body.replace(allowed_shape, forbidden, 1)
            self.assertNotEqual(tampered_body, body,
                                "the sample line vanished from the endpoint")
            self.assertTrue(_unexempted_table_mentions(tampered_body),
                            "%r inside a release scope went unreported" % forbidden)

        neighbour = self.function_source(server._sdr_ws_encode)
        head = 'return "text", json.dumps'
        polluted = neighbour.replace(
            head,
            "if len(_audio_tx_" + "profiles) > 1:\n        " + head, 1)
        self.assertIn("len(_audio_tx_profiles)", polluted,
                      "the out-of-scope sample never got dirty")
        for name, kind, target, must_contain in self.RELEASE_SCOPES:
            clean = self.scope(name, kind, target, must_contain)
            hits = _unexempted_table_mentions(clean)
            self.assertFalse(hits,
                             "%s trips on code outside its own scope: %s"
                             % (name, "; ".join(h[:80] for h in hits)[:160]))

    def test_legacy_uplink_branches_keep_their_default_rate(self):
        """Only 0x02 names a rate, and only its branch grew new handling.

        The local-microphone socket and CQ/FT8 always feed the default 48 kHz,
        so their long-standing early-return-on-no-handler stays theirs: this
        pins that no other call site in the endpoint acquired an explicit
        rate or a force-RX, which is what keeps finding-2's contract honest
        instead of aspirational.
        """
        body = self.tx_body()
        feeds = re.findall(r"_feed_tx_from_uplink\([^)]*\)", body)
        self.assertEqual(len(feeds), 4, "Opus, PCM, PCM16 and untagged legacy")
        self.assertEqual([f for f in feeds if "input_rate" in f],
                         ["_feed_tx_from_uplink(frame, input_rate=%d)" % PCM16_RATE])
        self.assertEqual(body.count("_force_rx_for_audio_error("), 2,
                         "the vanished-handler path and the conversion-failure "
                         "path — both inside the 0x02 branch only")

    def test_the_pcm16_branch_owns_both_release_paths(self):
        """The rate and the release are pinned together, inside 0x02 only.

        Silent-drop-on-failure would leave the carrier keyed, and dropping the
        explicit rate would make a PCM16 uplink play at a third speed — each
        half still looks plausible in review with the other one gone. So both
        live here: the call that names the rate, and the fact that every
        release this endpoint can write sits in that one branch (the two forms
        being a vanished handler and a failed conversion — see
        ``test_legacy_uplink_branches_keep_their_default_rate``).
        """
        body = self.tx_body()
        feed = f"_feed_tx_from_uplink(frame, input_rate={PCM16_RATE})"
        self.assertIn(feed, body)
        self.assertEqual(body.count("_force_rx_for_audio_error("), 2,
                         "exactly the vanished-handler and conversion-failure "
                         "paths, and nothing else may start releasing here")


if __name__ == "__main__":
    unittest.main()
