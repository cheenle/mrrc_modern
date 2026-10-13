# vendor-ft8lib — 快路径解码器（ft8_lib 的最小 C 子集）

- **上游**：https://github.com/kgoba/ft8_lib
- **钉住的 commit**：9fec6ca（`--depth 50` 检出，快路径仅依赖该 commit 的 ft8/、common/、fft/）
- **许可证**：MIT（见上游 LICENSE；本目录为原样复制，未做本地修改）
- **派生方式**：`tools/ft8/vendor_ft8lib.sh <checkout>` 复制并生成 `../vendor-ft8lib.sha256`
- **为什么是这些文件**：只需要「PCM → 瀑布 → 候选 → LDPC → 文本」这条链；
  `common/audio.c`、`common/wave.c`、`demo/`、`test/` 与本集成无关，故不 vendor。
- **本地修改**：无。任何需要改上游的地方必须走 patch 并在本节记录。
