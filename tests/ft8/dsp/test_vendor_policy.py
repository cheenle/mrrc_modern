"""Policy tests for the immutable WSJT-X vendor baseline."""

import hashlib
from pathlib import Path
import stat

import pytest


ROOT = Path(__file__).parents[3]
# Vendor baseline for patch reversibility = the pristine originals in
# ft8/dsp/vendor-refs (the seven files below are REPLACED by ../patched/, so
# they are deliberately absent from the compile subset in ../vendor).
# Per-file digest checking of the compile subset lives in
# test_vendor_completeness.py; this module checks patch reversibility and the
# stdcall helper's equivalence.
VENDOR_ROOT = ROOT / "ft8" / "dsp" / "vendor-refs"
VENDOR_STDCALL = ROOT / "ft8" / "dsp" / "vendor-refs" / "q65_set_list.f90"
HEADLESS_STDCALL = ROOT / "ft8" / "dsp" / "ft8_stdcall.f90"
PATCHED_COPY_CASES = [
    (
        VENDOR_ROOT / "ft8var" / "encode174_91var.f90",
        ROOT / "ft8" / "dsp" / "patched" / "encode174_91var.f90",
        b"include '/lib/ft8/ldpc_174_91_c_generator.f90'",
        b"include 'ldpc_174_91_c_generator.f90'",
    ),
    (
        VENDOR_ROOT / "ft8var" / "osd174_91var.f90",
        ROOT / "ft8" / "dsp" / "patched" / "osd174_91var.f90",
        b"include '/lib/ft8/ldpc_174_91_c_generator.f90'",
        b"include 'ldpc_174_91_c_generator.f90'",
    ),
    (
        VENDOR_ROOT / "ft8var" / "four2avar.f90",
        ROOT / "ft8" / "dsp" / "patched" / "four2avar.f90",
        b"include '/lib/fftw3.f90'",
        b"include 'fftw3.f90'",
    ),
]


def apply_exact_replacements(source: bytes, replacements: list[tuple[bytes, bytes]]) -> bytes:
    """Apply an allowlisted patch and reject an ambiguous vendor baseline."""
    result = source
    for old, new in replacements:
        assert result.count(old) == 1, old.decode(errors="replace")
        assert new not in result
        result = result.replace(old, new, 1)
    return result


AP_MASK_RESET = b"""  ! Headless requests do not retain AP masks from prior contexts.
  apsym=0; apsym(1)=99; apsym(30)=99
  apsymsp=0; apsymsp(1)=99; apsymsp(30)=99
  apsymdxns1=0; apsymdxns1(1)=99; apsymdxns1(30)=99
  apsymdxnsrrr=0; apsymdxnsrrr(1)=99; apsymdxnsrrr(30)=99
  apsymdxnsrr73=0; apsymdxnsrr73(1)=99; apsymdxnsrr73(30)=99
  apsymdxns73=0; apsymdxns73(1)=99; apsymdxns73(30)=99
  apsymdxstd=0; apsymdxstd(1)=99; apsymdxstd(30)=99
  apsymdxnsr73=0; apsymdxnsr73(1)=99; apsymdxnsr73(30)=99
  apsymdxns732=0; apsymdxns732(1)=99; apsymdxns732(30)=99
  apsymmyns1=0; apsymmyns1(1)=99
  apsymmyns2=0; apsymmyns2(1)=99; apsymmyns2(30)=99
  apsymmynsrrr=0; apsymmynsrrr(1)=99; apsymmynsrrr(30)=99
  apsymmynsrr73=0; apsymmynsrr73(1)=99; apsymmynsrr73(30)=99
  apsymmyns73=0; apsymmyns73(1)=99; apsymmyns73(30)=99
  apcqsym=0; apcqsym(1)=99; apcqsym(30)=99

"""


DECODE_CYCLE_VENDOR = b"""       if(ipass.eq.4) then
!$omp barrier
!$omp single
          if(npass.eq.9) then ! 3 decoding cycles
             nallocthr=nthr
             allocate(dd8m(180000), STAT = nAllocateStatus1)
             if(nAllocateStatus1.ne.0) STOP "Not enough memory"
             dd8m=dd8
          endif
          do i=1,179999
             dd8(i)=(dd8(i)+dd8(i+1))/2
          enddo
!$omp end single
!$omp barrier
       else if(ipass.eq.7) then
!$omp barrier
          if(nthr.eq.nallocthr) then
             dd8(1)=dd8m(1)
             do i=2,180000
                dd8(i)=(dd8m(i-1)+dd8m(i))/2
             enddo
             deallocate (dd8m, STAT = nDeAllocateStatus1)
             if (nDeAllocateStatus1.ne.0) print *, 'failed to release memory'
          endif
!$omp barrier
       endif
"""


DECODE_CYCLE_PATCHED = b"""       if(ipass.eq.4) then
!$omp barrier
          if(npass.eq.9) then ! 3 decoding cycles
             allocate(dd8m(180000), STAT = nAllocateStatus1)
             if(nAllocateStatus1.ne.0) STOP "Not enough memory"
             dd8m=dd8
          endif
          do i=1,179999
             dd8(i)=(dd8(i)+dd8(i+1))/2
          enddo
!$omp barrier
       else if(ipass.eq.7) then
!$omp barrier
          if(npass.eq.9) then
             dd8(1)=dd8m(1)
             do i=2,180000
                dd8(i)=(dd8m(i-1)+dd8m(i))/2
             enddo
             deallocate (dd8m, STAT = nDeAllocateStatus1)
             if (nDeAllocateStatus1.ne.0) print *, 'failed to release memory'
          endif
!$omp barrier
       endif
"""


DECODE_A8_VENDOR = b"""!$omp single            
! test code for a8 start
   if(lft8apon .and. ncontest.ne.6 .and. ncontest.ne.7 .and. la8 .and.          &
        len(trim(hiscall)).ge.3 .and.            &
        len(trim(hisgrid4)).ge.4 .and. ltry_a8) then
! Try for an a8 decode at nfqso
      f1=nfqso
      dxgrid=hisgrid4
      call timer('ft8_a8d ',0)
      call ft8_a8d(dd8,mycall,hiscall,dxgrid,f1,xdt,fbest,xsnr,plog,msg37) !w3sz was dd
      call timer('ft8_a8d ',1)
      if(msg37(1:1).ne.' ') then
         if(associated(this%callback)) then
            sync=10.0  !### ???   !URUR was 10, tried 15
            nsnr=nint(xsnr)
            iaptype=8
            qual=1.0
            if(plog.lt.-147.0) qual=0.16
            call this%callback(nsnr,xdt,fbest,msg37,iaptype,qual)
         endif
      endif
   endif
! test code for a8 stop    
!$omp end single nowait
"""


DECODE_A8_PATCHED = b"""!$omp barrier
   if(la8owner) then
!$omp atomic read
      ltry_a8_snapshot=ltry_a8
! test code for a8 start
      if(lft8apon .and. ncontest.ne.6 .and. ncontest.ne.7 .and. la8 .and.       &
           len(trim(hiscall)).ge.3 .and.                                       &
           len(trim(hisgrid4)).ge.4 .and. ltry_a8_snapshot) then
! Try for an a8 decode at nfqso
         f1=nfqso
         dxgrid=hisgrid4
         call timer('ft8_a8d ',0)
         call ft8_a8d(dd8,mycall,hiscall,dxgrid,f1,xdt,fbest,xsnr,plog,msg37) !w3sz was dd
         call timer('ft8_a8d ',1)
         if(msg37(1:1).ne.' ') then
            if(associated(this%callback)) then
               sync=10.0  !### ???   !URUR was 10, tried 15
               nsnr=nint(xsnr)
               iaptype=8
               qual=1.0
               if(plog.lt.-147.0) qual=0.16
               call this%callback(nsnr,xdt,fbest,msg37,iaptype,qual)
            endif
         endif
      endif
! test code for a8 stop
   endif
"""


CONCURRENCY_PATCH_CASES = [
    (
        VENDOR_ROOT / "ft8var" / "ft8_mod1.f90",
        ROOT / "ft8" / "dsp" / "patched" / "ft8_mod1.f90",
        [(b"  real*4 dd8(nps)\n", b"  real*4 dd8(nps)\n!$omp threadprivate(dd8)\n")],
    ),
    (
        VENDOR_ROOT / "ft8var" / "ft8_downsamplevar.f90",
        ROOT / "ft8" / "dsp" / "patched" / "ft8_downsamplevar.f90",
        [(b"  save cxx\n", b"  save cxx\n!$omp threadprivate(cxx)\n")],
    ),
    (
        VENDOR_ROOT / "ft8var" / "four2avar.f90",
        ROOT / "ft8" / "dsp" / "patched" / "four2avar.f90",
        [
            (b"  include '/lib/fftw3.f90'", b"  include 'fftw3.f90'"),
            (
                b"  save plan,nplan,nn,ns,nf,nl\n",
                b"  save plan,nplan,nn,ns,nf,nl\n!$omp threadprivate(plan,nplan,nn,ns,nf,nl)\n",
            ),
        ],
    ),
    (
        VENDOR_ROOT / "ft8var" / "osd174_91var.f90",
        ROOT / "ft8" / "dsp" / "patched" / "osd174_91var.f90",
        [
            (
                b"include '/lib/ft8/ldpc_174_91_c_generator.f90'",
                b"include 'ldpc_174_91_c_generator.f90'",
            ),
            (
                b"if(first_osd) then ! fill the generator matrix\n!$omp critical(first_osd)\n",
                b"!$omp critical(first_osd)\nif(first_osd) then ! fill the generator matrix\n",
            ),
            (
                b"!$omp end critical(first_osd)\nendif\n\nrx=llr\n",
                b"endif\n!$omp end critical(first_osd)\n\nrx=llr\n",
            ),
            (
                b"\n  return\nend subroutine fetchit91var",
                b"\n  return\nend subroutine fetchit91var\n",
            ),
        ],
    ),
    (
        VENDOR_ROOT / "ft8var" / "ft8apsetvar.f90",
        ROOT / "ft8" / "dsp" / "patched" / "ft8apsetvar.f90",
        [
            (
                b"  save hiscallprev,mycallprev,lhoundprev,first\n\n",
                b"  save hiscallprev,mycallprev,lhoundprev,first\n\n" + AP_MASK_RESET,
            ),
            (
                b"  if(hiscall.ne.hiscallprev .or. mycall.ne.mycallprev .or. (lhound.neqv.lhoundprev) .or. first) then ! first for lhound triggered",
                b"  if(.true.) then ! rebuild AP state for every headless request",
            ),
            (
                b"    if(hiscall.ne.hiscallprev) then\n",
                b"    if(.true.) then ! rebuild grid- and callsign-dependent masks\n",
            ),
        ],
    ),
    (
        VENDOR_ROOT / "ft8var" / "ft8_decodevar.f90",
        ROOT / "ft8" / "dsp" / "patched" / "ft8_decodevar.f90",
        [
            (
                b"       lft8apon,ncontest)\n",
                b"       lft8apon,ncontest,la8owner)\n",
            ),
            (
                b"         lmycallstd,lhiscallstd,lft8apon\n",
                b"         lmycallstd,lhiscallstd,lft8apon,la8owner\n",
            ),
            (b"    logical la8\n", b"    logical la8,ltry_a8_snapshot\n"),
            (DECODE_CYCLE_VENDOR, DECODE_CYCLE_PATCHED),
            (DECODE_A8_VENDOR, DECODE_A8_PATCHED),
        ],
    ),
]


def extract_stdcall(source: str) -> str:
    """Extract exactly one complete ``stdcall`` subroutine, or fail closed."""
    lines = source.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.lstrip().lower().startswith("subroutine stdcall(")
    ]
    ends = [
        index
        for index, line in enumerate(lines)
        if line.strip().lower() == "end subroutine stdcall"
    ]
    assert len(starts) == 1, f"expected one stdcall start marker, found {len(starts)}"
    assert len(ends) == 1, f"expected one stdcall end marker, found {len(ends)}"
    assert starts[0] < ends[0], "stdcall end marker must follow its start marker"
    return "".join(lines[starts[0] : ends[0] + 1])


def tree_digest(root: Path, base: Path = ROOT) -> str:
    """Return the stable digest for regular files below ``root``."""
    files = []
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        relative_path = path.relative_to(base).as_posix()
        if not stat.S_ISREG(mode):
            raise AssertionError(f"non-regular vendor entry: {relative_path}")
        files.append(path)

    files = sorted(
        files,
        key=lambda path: path.relative_to(base).as_posix().encode("utf-8"),
    )
    outer_digest = hashlib.sha256()
    for path in files:
        inner_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative_path = path.relative_to(base).as_posix()
        outer_digest.update(
            f"{inner_digest}  {relative_path}\n".encode()
        )
    return outer_digest.hexdigest()


def test_vendor_digest_rejects_non_regular_entries(tmp_path: Path) -> None:
    """Symlinks must fail closed instead of hashing their target contents."""
    regular_file = tmp_path / "source.txt"
    regular_file.write_text("vendor source")
    (tmp_path / "source-link").symlink_to(regular_file)

    with pytest.raises(AssertionError, match="non-regular vendor entry.*source-link"):
        tree_digest(tmp_path, base=tmp_path)


def test_every_patched_copy_has_a_pristine_vendor_ref() -> None:
    """vendor-refs must carry the original of every ../patched/ copy."""

    patched_dir = ROOT / "ft8" / "dsp" / "patched"
    missing = sorted(
        p.name for p in patched_dir.glob("*.f90")
        if not (VENDOR_ROOT / "ft8var" / p.name).is_file()
    )
    assert missing == []


def test_headless_stdcall_is_exact_vendor_extraction() -> None:
    """The isolated helper must remain byte-for-byte vendor equivalent."""
    expected = extract_stdcall(VENDOR_STDCALL.read_text(encoding="utf-8"))
    actual = HEADLESS_STDCALL.read_text(encoding="utf-8")

    assert actual == expected


@pytest.mark.parametrize(
    ("vendor_path", "patched_path", "vendor_include", "local_include"),
    PATCHED_COPY_CASES,
    ids=("encode174_91var", "osd174_91var", "four2avar"),
)
def test_improved_patched_copy_has_exactly_one_registered_include_change(
    vendor_path: Path,
    patched_path: Path,
    vendor_include: bytes,
    local_include: bytes,
) -> None:
    vendor = vendor_path.read_bytes()
    patched = patched_path.read_bytes()

    assert vendor.count(vendor_include) == 1
    assert vendor.count(local_include) == 0
    assert patched.count(vendor_include) == 0
    assert patched.count(local_include) == 1
    if patched_path.name == "encode174_91var.f90":
        assert patched.replace(local_include, vendor_include, 1) == vendor


@pytest.mark.parametrize(
    ("vendor_path", "patched_path", "replacements"),
    CONCURRENCY_PATCH_CASES,
    ids=(
        "ft8_mod1",
        "ft8_downsamplevar",
        "four2avar_concurrency",
        "osd174_91var_concurrency",
        "ft8apsetvar",
        "ft8_decodevar",
    ),
)
def test_registered_concurrency_patch_is_exact_and_reversible(
    vendor_path: Path,
    patched_path: Path,
    replacements: list[tuple[bytes, bytes]],
) -> None:
    vendor = vendor_path.read_bytes()
    expected = apply_exact_replacements(vendor, replacements)
    patched = patched_path.read_bytes()

    assert patched == expected
    restored = patched
    for old, new in reversed(replacements):
        assert restored.count(new) == 1
        restored = restored.replace(new, old, 1)
    assert restored == vendor
