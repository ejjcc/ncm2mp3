---
name: ncm2mp3
description: 本地音乐库处理工具集。用于网易云音乐加密文件（.ncm）解密/转换为可播放音频、音频格式转换（flac/m4a/wav 转 mp3，或互转，含码率与标签保留）、音乐库去重与重复曲目清理（同名不同码率、重复下载、改名副本）。当用户提到 ncm 解密、网易云加密文件打不开、批量转码、音乐文件夹里有重复歌曲要清理时使用。Local music library toolkit. Decrypts NetEase Cloud Music .ncm files into playable audio, transcodes between mp3/flac/m4a/wav while preserving tags and cover art, and de-duplicates a music folder using content hash unioned with normalized-title matching. Use when the user mentions ncm decryption, a NetEase download that will not open, batch audio conversion, or duplicate songs cluttering a music folder.
---

# ncm2mp3

本地音乐库的三项处理能力：ncm 解密、格式转换、去重。全部在本机文件系统上操作，不涉及设备同步、上传或云端服务。

脚本位于 `scripts/`，Python 3 标准库实现，外部依赖仅 `ncmdump`、`ffmpeg`、`ffprobe`。

## 前置条件（一次性配置）

1. `brew install taglib` —— 必须。ncmdump 的 macOS 预编译二进制动态链接 taglib，未安装时启动即失败，报 `Library not loaded: /opt/homebrew/opt/taglib/lib/libtag.2.dylib`。
2. 下载 ncmdump：https://github.com/taurusxin/ncmdump 的 release 1.5.1，本机（Apple Silicon）取 `ncmdump-1.5.1-macos-arm64.zip`（另有 linux-amd64 / windows-amd64）。解压后放入 PATH，或用环境变量 `NCM2MP3_NCMDUMP` 指向二进制绝对路径。
3. 清除隔离属性：从浏览器下载的二进制带 `com.apple.quarantine` xattr，需 `xattr -d com.apple.quarantine <path>` 后才能执行。
4. `brew install ffmpeg` —— 转码与去重报告的时长列依赖 ffmpeg / ffprobe。

`ncm_decode.py` 会在执行前预检 ncmdump 是否可运行，失败时直接打印上述 taglib 提示。

## 脚本

### scripts/ncm_decode.py

```
usage: ncm_decode.py [-o OUTPUT] [--recursive] [--keep-going] [--dry-run] INPUT [INPUT ...]
```

INPUT 可以是若干 .ncm 文件或目录。封装 ncmdump 二进制，逐文件输出 OK/FAIL，结束时给出汇总与输出格式分布（mp3 / flac 各多少）。`--keep-going` 表示单文件失败不中断整批。

```
python3 scripts/ncm_decode.py --recursive -o ~/Music/decoded ~/Music/网易云音乐
```

### scripts/transcode.py

```
usage: transcode.py --to {mp3,flac,m4a,wav} [-b BITRATE] [-o OUTPUT] [-j JOBS] [--replace] [--dry-run] INPUT [INPUT ...]
```

基于 ffmpeg。mp3 默认码率 320k。保留标签与内嵌封面。`-o` 与 `--replace` 互斥（同时给出报错并以退出码 2 结束）：前者把结果写到独立目录并保留源文件，后者就地覆盖源文件，语义冲突。已经是目标格式的文件默认跳过，除非给出 `--replace`。跳过发生在计算输出路径之前，所以 `-o` 目录只收到实际转换出来的文件，已是目标格式的源文件不会被复制过去——`-o` 目录不是一份完整音乐库，不能拿它当去重或播放的对象。`--replace` 只在新文件非空且可解码校验通过后才覆盖源文件。

覆盖规则：输出路径上已存在文件且它不是源文件本身时，该文件报 FAIL 跳过，不被覆盖（例：同目录已有 `Artist - Title.mp3`，再把 `Artist - Title.flac` 转 mp3 会被拒绝）。多个源映射到同一输出路径（`track.wav` 与 `track.flac` 都转 mp3）在派发任务前就被检出并全部标记 FAIL，不会让并发任务竞写同一路径。确实要替换已有文件时，先手动移走或改名再跑。

```
python3 scripts/transcode.py --to mp3 -b 320k -j 4 -o ~/Music/mp3 ~/Music/decoded
```

这条只把 `~/Music/decoded` 里非 mp3 的文件写进 `~/Music/mp3`；若解码产物本身就全是 mp3（见「陷阱」第一条），输出目录会是空的，汇总显示 `0 converted, N skipped`。要在一个目录里得到统一格式，改为就地转换：把非目标格式的文件作为 INPUT 传入并加 `--replace`（对已是 mp3 的文件用 `--replace` 会触发无意义的重编码，不要整目录传）。

### scripts/audio_dedup.py

```
usage: audio_dedup.py [--apply] [--quarantine DIR] [--recursive] [--report FILE] DIR
```

默认 dry-run：只打印分组报告，不改动任何文件。`--apply` 把非保留文件移入隔离目录（默认 `<DIR>/.dedup-quarantine`），并在隔离目录内按文件相对扫描根目录的原始子路径存放，最后打印还原命令 `rsync -a --ignore-existing '<隔离目录>/' '<扫描根>/'`：按原子路径逐个归位，`--ignore-existing` 保证不覆盖仍在原地的保留文件。自动跳过 `._*` AppleDouble 文件与隐藏目录（隔离目录本身也因此不会被再次扫描）。

```
python3 scripts/audio_dedup.py --recursive --report /tmp/dedup.txt ~/Music/decoded
python3 scripts/audio_dedup.py --recursive --apply ~/Music/decoded
```

## 去重算法

两路独立信号，用并查集合并，缺一不可（实测 133 文件库共 16 个重复组：仅 md5 命中 10 组，仅标题命中 10 组，交集只有 5 组）：

- 信号 A —— 内容哈希（文件字节 md5）。命中同一文件被存成不相干文件名的情况，例如 `Berwyn Gesaffelstein.mp3` 与 `Fred again..,Berwyn,Gesaffelstein - BerwynGesaffNeighbours.mp3`。漏掉同曲不同码率。
- 信号 B —— 归一化标题 token 集合。去扩展名，去尾部 `(1)` / `(2)` 副本后缀，NFKD 归一化并剥离组合符（使 `Tiesto` 等于 `Tiësto`），转小写，去停用词（feat、ft、featuring、original mix、edit、from、by、the），按 `[a-z0-9]` 加 CJK 切词，比较排序后的 token 集合（使 `Are You With Me - Lost Frequencies` 等于 `Lost Frequencies Are You With Me`）。命中重编码与作者/曲名顺序颠倒。漏掉文件名毫无共同词的情况。

保留者选择顺序：文件体积最大（码率最高）→ 文件名符合 `Artist - Title` 约定 → 文件名最短。

## 决策表

| 用户意图 | 走哪个脚本 |
| --- | --- |
| 网易云下载的歌打不开 / 后缀是 .ncm / 要解密 | `ncm_decode.py` |
| flac、m4a、wav 转 mp3，或压缩码率、统一格式 | `transcode.py` |
| 音乐文件夹里有重复歌曲 / 同一首存了好几份 / 清理重复 | `audio_dedup.py`（先 dry-run 看报告） |
| 整盘网易云文件夹整理 | 先 `ncm_decode.py` 解码，再对解码目录里非目标格式的文件跑 `transcode.py`，最后对**解码目录本身**跑 `audio_dedup.py`（`-o` 目录缺少已是目标格式的文件，不能作为去重对象） |

## 陷阱

- **ncm 容器内可能是 FLAC 而非 MP3。** ncmdump 按实际负载写出扩展名。某次 75 文件实跑全部产出 mp3，但不能据此假定；后续步骤必须探测真实扩展名，不要硬编码 `.mp3`。
- **AppleDouble `._` 文件会让所有计数翻倍。** macOS 在 FAT/exFAT 卷上为每个文件写一个 `._<name>` 元数据伴生文件（约 4KB，不是音频）。任何扫描音乐目录的逻辑都必须跳过 basename 以 `._` 开头的文件。
- **时长不是有效的去重信号。** 同一个 133 文件库上，按整秒时长匹配得到 26 个"组"，其中只有 6 组是真重复——三四分钟区间的不相干曲目大量碰撞。时长只能作为报告里的展示列，或对已由信号 A / B 形成的组做二次确认，不能用来分组。
- **隔离而非删除。** 可移动卷没有废纸篓，误删不可恢复。`--apply` 只做 move 进隔离目录，任何情况下都不做硬删除。还原用脚本打印的 `rsync -a --ignore-existing '<隔离目录>/' '<扫描根>/'`，它依赖隔离目录里保留的相对子路径把每个文件放回原来的子目录。不要用 `mv '<隔离目录>'/* '<扫描根>'/` 这类扁平 move：递归扫描时它会把子目录里的文件全部堆进扫描根，同名文件还会直接覆盖掉被选为保留者的高码率副本。
