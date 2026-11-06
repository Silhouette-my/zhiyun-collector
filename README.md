# Zhiyun Collector

智云课堂独立采集插件，供 Course Compiler 的通用插件宿主调用。当前版本 **0.3.0.dev3**，稳定工具 ID 为 `handouter-zhiyun-collector`，保留 v1 浏览器 ZIP 转换与 v2 登录、课程浏览及直接采集接口。

这是**公开可见的源码（source-available），不是开源授权项目**。自有代码保留所有权利，使用、修改和再分发需取得许可；第三方 MIT 部分及适用范围见 [NOTICE](src/handouter_zhiyun_collector/THIRD_PARTY_NOTICES.txt) 和 [LICENSE](LICENSE)。GitHub 平台条款授予的权利仍适用。

## 在 GUI 中使用

1. 取得维护者提供的、匹配平台的独立插件发行目录及可信渠道发布的清单摘要。
2. 在 Course Compiler 设置中安装 `plugin-manifest.json`；核对发布者及摘要，不把旁边的摘要文件本身当作发布者信任。
3. 新建讲义，选择插件并登录，搜索课程、选择讲次，查看并确认下载域名。
4. 插件在本机下载课件图和视频，原子发布标准 ZIP。本体必须经完整 `local_inspect` 后才能转写。
5. 下载与登录不授权云上传。只有本体整理出的白名单材料，在用户另行确认后才可以提交云端。

v1 用户也可以继续通过配套 [浏览器脚本](src/handouter_zhiyun_collector/zhiyun_exporter.user.js) 导出 ZIP，再由插件预检和转换。脚本 ZIP 含私有下载地址，只在本机保存，禁止上传或提交到仓库。

浏览器 ZIP 按讲次命名，如 `普通物理学Ⅱ（H）26-09-17_第一周_周四第1-2节.zip`（课程名紧接两位年份的日期，周次用中文数字）。课程名优先取接口的 `course_title`，否则去掉页面标题末尾的学年。日期、开课时间和节次取自当前讲次的 `get-sub-info`（`start_at`/`end_at` 按北京时间换算，节次解析自 `sub_title`），接口不可用时退回页面状态。智云没有教学周字段，周次按课次目录（`course/catalogue`）中本课程第一节课所在周为第 1 周推算；如需按校历，可在课程页控制台执行 `localStorage.setItem('zhiyunExporter.termStartMonday', 'YYYY-MM-DD')` 指定第 1 周周一。推算不出周次时省略周次段，没有节次时改用开课时间，连上课日期也识别不到时改用 `课程名_导出YYYYMMDD_HHMM.zip` 并在面板提示。同样的信息写入 `course_info.json` 的 `lecture` 字段；插件只读取 `courseName` 与 `lectureDate`，不依赖文件名。页面还没给出回放视频地址，或 PPT 切换时间全部未知（常见于连续快速导出、页面未加载完）时，脚本不生成 ZIP 并提示等待加载后重试；旧包缺少媒体地址时插件返回 `COLLECTOR_NO_MEDIA`。

## 从 Release 安装（普通用户）

维护者推送版本标签后，[Release 页面](https://github.com/Silhouette-my/zhiyun-collector/releases) 会附上 Windows、macOS 的预构建插件和 `SHA256SUMS`。只从这个仓库的 Release 页面下载；别处转发的文件和摘要都不算可信来源。

1. 下载与本机平台对应的 `handouter-zhiyun-collector-<版本>-windows-x64.zip`（Apple 芯片 Mac 用 `macos-arm64`），以及同一版本的 `SHA256SUMS`。
2. 解压后得到文件夹 `handouter-zhiyun-collector-<版本>-windows-x64`（下称“解压目录”），把它放到长期不动的位置，例如 `%LOCALAPPDATA%\Programs\` 下。不要在“下载”文件夹或压缩包预览里直接运行；以后移动目录需要重新注册。
3. 核对摘要。`SHA256SUMS` 中 `.../handouter-zhiyun-collector.exe`（macOS 为不带后缀的 `.../handouter-zhiyun-collector`）那一行就是程序的 SHA-256：

   ```powershell
   # Windows PowerShell；输出为大写，与 SHA256SUMS 中的小写比较时忽略大小写
   (Get-FileHash "$env:LOCALAPPDATA\Programs\handouter-zhiyun-collector-<版本>-windows-x64\handouter-zhiyun-collector.exe" -Algorithm SHA256).Hash
   ```

   ```bash
   # macOS
   shasum -a 256 /absolute/handouter-zhiyun-collector
   ```

   摘要不一致就删掉文件重新下载，不要运行。
4. 在**自己的终端**里注册一次（用核对过的那串摘要）：

   ```powershell
   course-compiler collector-configure --executable "<解压目录>\handouter-zhiyun-collector.exe" --sha256 <SHA256SUMS 中 exe 那一行的摘要> --tool-id handouter-zhiyun-collector --exporter-script "<解压目录>\zhiyun_exporter.user.js"
   ```

5. 把同目录的 `zhiyun_exporter.user.js` 装进浏览器的油猴（Tampermonkey）扩展，用于导出浏览器 ZIP。

插件尚未代码签名或公证，Windows SmartScreen、macOS Gatekeeper 可能提示来源未验证；SHA-256 只证明文件与 Release 页面发布的一致，不等于签名。每个新版本的摘要都不同，升级后要重新运行 `collector-configure`。

## 在 TUI／命令行中使用

TUI 不再内置网站解析或浏览器脚本。安装匹配平台的独立插件后，先导出配套脚本并注册一次：

```bash
handouter-zhiyun-collector export-script --output /absolute/new/zhiyun_exporter.user.js
course-compiler collector-configure --executable /absolute/handouter-zhiyun-collector --sha256 <可信发布者提供的二进制摘要> --tool-id handouter-zhiyun-collector --exporter-script /absolute/new/zhiyun_exporter.user.js
```

之后继续把浏览器 ZIP 放进原 TUI 的 Source File。TUI 先调用独立插件预检，展示精确媒体域名；确认后才下载，标准包完整预检成功后再进入本地转写。安装插件及下载均不等于同意上传。TXT、音视频和标准包不需要采集插件。

纯命令行可以先预检，再明确给出本次确认的域名：

```bash
course-compiler collector-inspect /absolute/browser-export.zip
course-compiler run --asset /absolute/browser-export.zip --allowed-media-host media.example.org --agent manual
```

没有安装 Course Compiler 时，也可单独使用插件转换：

```bash
handouter-zhiyun-collector inspect --browser-export /absolute/browser-export.zip
handouter-zhiyun-collector --browser-export /absolute/browser-export.zip --allowed-media-host media.example.org --output /absolute/new-standard.zip
```

Windows 使用同样的参数名，并将程序路径替换为 `.exe`；路径作为独立参数传递，不拼接 shell。输出文件必须尚不存在。TUI 插件注册保存在用户 Handouter 配置目录的 `collector.json`，不会从 ZIP 提取软件入口。

## 经授权的本地开发

Python 3.11+；原生登录依赖系统 WebView 和系统凭证库。macOS 使用 Keychain，Windows 使用 Windows Credential Manager，不回退明文凭证文件。

```bash
python -m venv .venv
# 激活本地虚拟环境后：
python -m pip install -e .
python -m unittest discover -s tests -v
node --check src/handouter_zhiyun_collector/zhiyun_exporter.user.js
```

无 GUI 的协议测试只需 `certifi==2026.7.22`，不登录学校，也不下载真实课程。标准包测试是独立结构断言，不替代 Course Compiler 的完整导入校验。平台本机登录、真实课程到讲义全链路及 Windows 原生安装尚未验收。

### 构建独立制品

```bash
python scripts/build_collector.py --output-dir dist/wheel
python scripts/build_collector_plugin.py --output-dir dist/native
```

原生插件必须在对应 macOS/Windows 主机构建。脚本使用临时隔离环境、固定依赖、白名单源码构建，不依赖原开发仓库。输出包含单文件程序、协议清单、浏览器脚本及许可证。构建目录必须是新的路径；不会覆盖已有制品。Windows PowerShell 5.1 会把 pip、PyInstaller 写到 stderr 的普通信息显示成红色 `NativeCommandError`，这不代表失败，以退出码和输出目录里的产物为准。

[测试 CI](.github/workflows/test.yml) 在 macOS、Windows 运行合成测试。[原生构建](.github/workflows/build.yml) 由维护者手动触发，只上传 Actions artifact。

[发布流程](.github/workflows/release.yml)：维护者只改 `src/handouter_zhiyun_collector/__init__.py` 里的 `__version__`（`pyproject.toml` 和构建脚本都从这里读，依赖版本只写在 `pyproject.toml`），合入 main 后推送标签 `v<版本>`（如 `git tag v0.3.0 && git push origin v0.3.0`）。工作流先核对标签与版本号一致，在 Windows、macOS 上跑合成测试、构建并做身份探测，再用 `scripts/package_release.py` 打包，最后由唯一有 `contents: write` 权限的任务汇总 `SHA256SUMS`（列出每个 ZIP 及其中 exe、清单等文件的摘要）、逐项校验后创建 GitHub Release。带 `dev`、`a`、`b`、`rc` 后缀的版本标为预发布。制品尚未签名/公证；清单摘要不等于签名。

## 接口和安全边界

详见 [v1/v2 协议](docs/PROTOCOL.md) 和 [标准包输出](docs/STANDARD_PACKAGE.md)。宿主通过参数数组启动明确配置的程序；每次一行 JSON 请求，stdout 只输出有界 NDJSON，不通过 shell。`0.3.0.dev3` 起身份能力含 `progress_bytes`：宿主在 `convert`（v2 为 `collect`）请求里加 `"progress_bytes": true` 后，下载阶段的进度帧会带 `completed_bytes`、`total_bytes`；不加则输出与旧版相同。

登录、网站接口、签名 URL、Cookie 和媒体下载全部在此插件中；本体只接收脱敏目录、计划及结果路径。HTTPS 证书校验、精确域名确认、连接 IP 固定和拒绝媒体重定向不能由宿主绕过。仅采集用户依法有权访问的资源，不绕过登录、课程权限或 DRM。

## 来源

课程和课件 API 参考 [zju-learning-cli](https://github.com/8eoyw/zju-learning-cli)；回放元数据与签名算法参考 [zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)。具体固定版本、MIT 版权及完整条款见 NOTICE。本仓库不含 Course Compiler 私有生成服务、写作规则、模型提示或真实课程夹具。
