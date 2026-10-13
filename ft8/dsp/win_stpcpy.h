/* MinGW-w64 provides no stpcpy(), and ft8_lib calls it.
 *
 * The ft8_lib subset (vendor-ft8lib/ft8/message.c) uses stpcpy(), which is
 * POSIX 2008 and present on Linux and macOS -- but MSYS2's mingw-w64 has no
 * declaration for it anywhere in its headers (only libunistring's unistr.h
 * even mentions the name) and the MSVCRT/UCRT runtime it links against has no
 * such function.  Upstream ft8_lib builds on Windows, so the call is legal
 * there; it just resolves against a compiler builtin that GCC 14 refuses
 * (-Werror=implicit-function-declaration is the default since GCC 14, and an
 * implicitly declared stpcpy would also return a truncated 32-bit pointer on
 * x64 -- a real crash, not a style complaint).
 *
 * Vendor files are never patched (that is what the SHA-256 manifests next to
 * them are for), so the missing function is supplied here instead: a static
 * inline definition, force-included into every C translation unit on Windows
 * by ft8/dsp/CMakeLists.txt.  A static inline in a header means no new linker
 * symbol and no separate object file; `unused` keeps the TUs that do not call
 * it quiet.
 *
 * The signature is the standard one, which is also the shape GCC knows as a
 * builtin, so no -Wbuiltin-declaration-mismatch warning is produced.
 */
#ifndef MRRC_FT8_WIN_STPCPY_H
#define MRRC_FT8_WIN_STPCPY_H

#if defined(_WIN32)

__attribute__((unused))
static inline char *stpcpy(char *dst, const char *src) {
    while ((*dst = *src) != '\0') {
        ++dst;
        ++src;
    }
    return dst;
}

#endif /* _WIN32 */

#endif /* MRRC_FT8_WIN_STPCPY_H */
