# ncm2mp3

本地音乐库处理工具：一个 Claude Code Skill，外加三个可独立运行的 Python 脚本。

覆盖三件事：

1. **解密**网易云音乐 `.ncm` 文件（调用第三方 ncmdump 二进制）
2. **转码**音频格式（基于 ffmpeg）
3. **去重**音乐目录（双信号 + 并查集，默认 dry-run）

不包含设备同步、USB 传输、云端上传等能力，也不打算加入。

安装为 Skill 后，可以直接对 Claude Code 说「把这个目录里的 ncm 解密并去重」，由 Skill 决定调用哪个脚本；也可以完全绕开 Claude Code，把 `scripts/` 当普通命令行工具使用。

## 目录结构

```
ncm2mp3/
├── SKILL.md            Skill 定义，供 Claude Code 读取
├── README.md
├── LICENSE
├── references/         补充说明文档
└── scripts/
    ├── ncm_decode.py   .ncm 解密（ncmdump 封装）
    ├── transcode.py    格式转换（ffmpeg 封装）
    └── audio_dedup.py  目录去重（纯标准库）
```

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/<your-account>/ncm2mp3.git ~/Developer/ncm2mp3
```

### 2. 注册为 Claude Code Skill

软链接（推荐，仓库更新后 Skill 自动跟进）：

```bash
mkdir -p ~/.claude/skills
ln -s ~/Developer/ncm2mp3 ~/.claude/skills/ncm2mp3
```

或复制一份（需要手动同步更新）：

```bash
cp -R ~/Developer/ncm2mp3 ~/.claude/skills/ncm2mp3
```

只想用命令行脚本的话，这一步可以跳过。

### 3. 安装系统依赖

```bash
brew install taglib ffmpeg
```

- `taglib`：macOS 版 ncmdump 二进制动态链接 `libtag.2.dylib`，未安装时程序启动即失败，报 `Library not loaded: /opt/homebrew/opt/taglib/lib/libtag.2.dylib`。
- `ffmpeg`：`transcode.py` 依赖 `ffmpeg` 与 `ffprobe`。

### 4. 安装 ncmdump

从 [taurusxin/ncmdump](https://github.com/taurusxin/ncmdump) 的 Releases 页面下载 1.5.1 对应平台的资产：macOS arm64 为 `ncmdump-1.5.1-macos-arm64.zip`，另有 linux-amd64 与 windows-amd64 版本。

解压后放到 PATH 上，并清除 macOS 隔离属性：

```bash
unzip ncmdump-1.5.1-macos-arm64.zip
chmod +x ncmdump
xattr -d com.apple.quarantine ncmdump 2>/dev/null || true
mv ncmdump /usr/local/bin/
```

不想放进 PATH 时，用环境变量指定路径：

```bash
export NCM2MP3_NCMDUMP=/path/to/ncmdump
```

验证：

```bash
ncmdump --version
```

`ncm_decode.py` 启动时会做一次预检，若 ncmdump 无法运行会直接打印 taglib 安装提示。

Python 侧无需 `pip install`，三个脚本只用标准库。

## 使用

三个脚本互相独立，可单独调用，也可按「解密 → 转码 → 去重」顺序串联。

### ncm_decode.py — 解密 .ncm

```
usage: ncm_decode.py [-o OUTPUT] [--recursive] [--keep-going] [--dry-run] INPUT [INPUT ...]
```

`INPUT` 可以是任意数量的 `.ncm` 文件或目录。

```bash
# 单个文件，输出到源文件所在目录
python3 scripts/ncm_decode.py "~/Music/网易云音乐/Lost Frequencies - Are You With Me.ncm"

# 整个目录递归处理，统一输出到 ~/Music/decoded
python3 scripts/ncm_decode.py --recursive -o ~/Music/decoded ~/Music/网易云音乐

# 先看会处理哪些文件，不实际执行
python3 scripts/ncm_decode.py --dry-run --recursive ~/Music/网易云音乐

# 单个文件失败不中断整批
python3 scripts/ncm_decode.py --keep-going --recursive ~/Music/网易云音乐
```

逐文件输出 OK / FAIL，结束时给出汇总和输出格式分布。

`.ncm` 容器内可能是 MP3 也可能是 FLAC，ncmdump 按实际负载写出对应格式，不能预设扩展名。实测一批 75 个文件全部产出 `.mp3`，但这只是那批文件的构成，不是规律。

### transcode.py — 格式转换

```
usage: transcode.py --to {mp3,flac,m4a,wav} [-b BITRATE] [-o OUTPUT] [-j JOBS] [--replace] [--dry-run] INPUT [INPUT ...]
```

```bash
# FLAC 转 MP3，默认 320k
python3 scripts/transcode.py --to mp3 ~/Music/decoded

# 指定码率与输出目录，4 路并行
python3 scripts/transcode.py --to mp3 -b 192k -o ~/Music/mp3 -j 4 ~/Music/decoded

# 转 m4a 并覆盖源文件
python3 scripts/transcode.py --to m4a --replace ~/Music/decoded

# 预览命令，不执行
python3 scripts/transcode.py --to flac --dry-run ~/Music/decoded
```

保留标签与内嵌封面。已是目标格式的文件默认跳过，除非显式给 `--replace`。`--replace` 仅在新文件通过非空与可解码校验后才覆盖源文件。`-o` 与 `--replace` 互斥，同时给出会报错并以退出码 2 结束。

输出文件权限跟随源文件（`tempfile.mkstemp` 默认产出 0600，脚本会改回源文件的模式），避免转码后整个音乐库变成仅自己可读。

不覆盖既有文件：目标路径已存在且不是源文件本身时，该文件报 FAIL（`destination already exists, not overwritten`），既不覆盖也不备份。同一批次内多个源映射到同一目标路径时——例如上面 `~/Music/decoded` 这类 mp3/flac 混合目录里同时存在 `Track.flac` 与 `Track.mp3`，或 `Track.flac` 与 `Track.wav` 都转 `Track.mp3`——冲突在提交并行任务之前检出，涉及的源文件全部报 FAIL，一个都不转换，`-j` 并行下也不会产生不确定结果。两种情况批次退出码均为 1；先重命名冲突文件，或用 `-o` 把结果写到独立目录，再重跑。

### audio_dedup.py — 目录去重

```
usage: audio_dedup.py [--apply] [--quarantine DIR] [--recursive] [--report FILE] DIR
```

```bash
# 默认 dry-run：打印分组报告，不改动任何文件
python3 scripts/audio_dedup.py ~/Music/decoded

# 递归扫描并把报告写入文件
python3 scripts/audio_dedup.py --recursive --report ~/dedup-report.txt ~/Music/decoded

# 确认报告无误后执行：非保留文件移入隔离目录
python3 scripts/audio_dedup.py --apply ~/Music/decoded

# 指定隔离目录位置
python3 scripts/audio_dedup.py --apply --quarantine ~/Music/dedup-trash ~/Music/decoded
```

安全约定：

- 默认 dry-run，不加 `--apply` 时只读。
- `--apply` 只做 move，不做 delete。默认隔离目录为 `<DIR>/.dedup-quarantine`，非保留文件按相对扫描根的路径存放（`sub/song.mp3` 移入 `<quarantine>/sub/song.mp3`），不做扁平化，因此递归扫描下不同子目录的同名文件不会互相覆盖。执行后打印可直接粘贴的还原命令 `rsync -a --ignore-existing '<quarantine>'/ '<DIR>'/`：按原目录层级还原，`--ignore-existing` 保证仍在原位的保留文件不会被隔离目录里的低码率副本覆盖。
- 移动盘没有废纸篓，因此不提供硬删除选项。
- 跳过 `._` 开头的 AppleDouble 文件和隐藏目录。macOS 在 FAT/exFAT 卷上会为每个文件写一个 `._<name>` 元数据存根（约 4KB，不是音频），不跳过会让所有统计数字翻倍。

## 去重算法：为什么用两个信号

这是本仓库唯一不直观的部分，值得单独说明。

去重用两个彼此独立的信号，再用并查集（union-find）合并成组。只用其中一个都会漏掉约一半重复项——在一个 133 个文件的真实音乐库上实测：总共 16 组重复，md5 单独找出 10 组，标题归一化单独找出 10 组，两者交集只有 5 组。

**信号 A：内容哈希（文件字节的 md5）**

抓的是同一份文件以不同文件名保存的情况，例如 `Berwyn Gesaffelstein.mp3` 与 `Fred again..,Berwyn,Gesaffelstein - BerwynGesaffNeighbours.mp3` 字节完全相同。局限是对同曲不同码率无效——重新编码后字节全变。

**信号 B：标题归一化词集**

处理流程：去扩展名 → 去掉末尾的 `(1)` / `(2)` 副本后缀 → NFKD 归一化并丢弃组合记号（使 `Tiesto` 与 `Tiësto` 等价）→ 转小写 → 去停用词（feat、ft、featuring、original mix、edit、from、by、the）→ 按 `[a-z0-9]` 与 CJK 字符切词 → 比较**排序后的词集合**。

因为比的是集合而非序列，`Are You With Me - Lost Frequencies` 与 `Lost Frequencies Are You With Me` 判为同组。这个信号抓的是重新编码和艺术家/标题顺序颠倒的情况，局限是文件名之间毫无共同词汇时失效。

**合并与保留策略**

两个信号的分组结果用并查集取并集，每组按以下顺序选保留项：

1. 文件体积最大（码率最高）
2. 文件名符合 `Artist - Title` 约定
3. 文件名最短（最终 tiebreak）

**反模式：不要用音频时长做分组信号**

整数秒时长碰撞绝大多数是误判。同一个 133 文件库上，按时长匹配产出 26 个「组」，其中只有 6 组是真重复——不相关的曲目在 3 到 4 分钟区间持续碰撞。时长只适合两种用法：作为展示列，或作为对信号 A / B 已形成分组的确认性检查。

## 兼容性

开发与验证均在 macOS arm64 上完成。三个脚本只使用 Python 3 标准库，路径处理遵循 POSIX 约定，文件名中的空格、引号、Unicode 与 CJK 字符均已处理，理论上可在 Linux 运行，但只有 macOS 路径经过实际执行验证。Linux 与 Windows 使用者需要自行下载对应平台的 ncmdump 资产，并自行验证依赖链接情况（taglib 相关的启动失败与隔离属性问题是 macOS 特有的）。

## 法律声明

本仓库不包含任何解密算法，也不包含任何受版权保护的音频文件。

`ncm_decode.py` 仅是对第三方 ncmdump 二进制的命令行封装：它负责路径遍历、批量调度、错误汇总与输出统计，解密逻辑全部位于 ncmdump 内部，本仓库既不实现也不分发该二进制。

使用者需自行确认对所处理文件拥有相应权利，仅可转换自己有权转换的文件。因使用本工具产生的任何法律责任由使用者自行承担。

## License

MIT，详见 [LICENSE](LICENSE)。
