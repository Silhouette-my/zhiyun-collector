# Zhiyun Collector

智云课堂独立采集插件，供 Course Compiler 的通用插件宿主调用。当前版本 **0.3.0.dev2**，稳定工具 ID 为 `handouter-zhiyun-collector`，保留 v1 浏览器 ZIP 转换与 v2 登录、课程浏览及直接采集接口。

这是**公开可见的源码（source-available），不是开源授权项目**。自有代码保留所有权利，使用、修改和再分发需取得许可；第三方 MIT 部分及适用范围见 [NOTICE](src/handouter_zhiyun_collector/THIRD_PARTY_NOTICES.txt) 和 [LICENSE](LICENSE)。GitHub 平台条款授予的权利仍适用。

## 在 GUI 中使用

1. 取得维护者提供的、匹配平台的独立插件发行目录及可信渠道发布的清单摘要。
2. 在 Course Compiler 设置中安装 `plugin-manifest.json`；核对发布者及摘要，不把旁边的摘要文件本身当作发布者信任。
3. 新建讲义，选择插件并登录，搜索课程、选择讲次，查看并确认下载域名。
4. 插件在本机下载课件图和视频，原子发布标准 ZIP。本体必须经完整 `local_inspect` 后才能转写。
5. 下载与登录不授权云上传。只有本体整理出的白名单材料，在用户另行确认后才可以提交云端。

v1 用户也可以继续通过配套 [浏览器脚本](src/handouter_zhiyun_collector/zhiyun_exporter.user.js) 导出 ZIP，再由插件预检和转换。脚本 ZIP 含私有下载地址，只在本机保存，禁止上传或提交到仓库。

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

原生插件必须在对应 macOS/Windows 主机构建。脚本使用临时隔离环境、固定依赖、白名单源码构建，不依赖原开发仓库。输出包含单文件程序、协议清单、浏览器脚本及许可证。构建目录必须是新的路径；不会覆盖已有制品。

[测试 CI](.github/workflows/test.yml) 在 macOS、Windows 运行合成测试。[原生构建](.github/workflows/build.yml) 仅允许维护者手动触发，上传 Actions artifact，不创建 Release、不自动发布。制品尚未签名/公证；清单摘要不等于签名。

## 接口和安全边界

详见 [v1/v2 协议](docs/PROTOCOL.md) 和 [标准包输出](docs/STANDARD_PACKAGE.md)。宿主通过参数数组启动明确配置的程序；每次一行 JSON 请求，stdout 只输出有界 NDJSON，不通过 shell。

登录、网站接口、签名 URL、Cookie 和媒体下载全部在此插件中；本体只接收脱敏目录、计划及结果路径。HTTPS 证书校验、精确域名确认、连接 IP 固定和拒绝媒体重定向不能由宿主绕过。仅采集用户依法有权访问的资源，不绕过登录、课程权限或 DRM。

## 来源

课程和课件 API 参考 [zju-learning-cli](https://github.com/8eoyw/zju-learning-cli)；回放元数据与签名算法参考 [zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)。具体固定版本、MIT 版权及完整条款见 NOTICE。本仓库不含 Course Compiler 私有生成服务、写作规则、模型提示或真实课程夹具。
