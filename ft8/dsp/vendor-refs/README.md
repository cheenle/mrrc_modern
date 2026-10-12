# vendor-refs — 参考素材（不参与编译）

这里的文件全部来自同一份 WSJT-X 3.0.2 Improved 检出（`lib/`），**不进编译**
（编译源在 `../vendor/`，由 `tools/ft8/extract_wsjtx_subset.sh` 从 CMake 清单派生）。

它们只为两类回归提供"原始基线"：

1. **7 个 patched 副本的逆替换验证**（`tests/ft8/dsp/test_vendor_policy.py`）：
   `ft8var/{encode174_91var, osd174_91var, four2avar, ft8_mod1, ft8_decodevar,
   ft8_downsamplevar, ft8apsetvar}.f90` 是被 `../patched/` 同名字文件替代的原件，
   因此不在编译清单里。
2. **`ft8_stdcall.f90` 的字节级等价性**（同上测试）：
   `q65_set_list.f90` 是 `../ft8_stdcall.f90` 的提取来源。
