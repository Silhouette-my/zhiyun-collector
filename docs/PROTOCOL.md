# 采集插件开发接口

## v2：直接浏览和采集资源

v2 将在线资源获取接入同一个本机宿主。网站登录、API、页面解析和下载都在独立插件内；桌面只渲染通用目录、资源、采集计划和任务。插件不执行转写或提交云端任务。下文 v1 的 ZIP 接口保持原样，旧插件不必实现登录或升级。

### 安装与版本选择

普通用户在设置中选择独立发行目录的 `plugin-manifest.json`，提供从可信发布渠道取得的**清单 SHA-256**。宿主先验证清单，再检查平台、协议及同目录单个原生可执行文件，将核验后的字节复制到应用数据目录 `plugins/<tool_id>/<version>-<installation_id>/`。不会执行安装脚本、扫描程序或从课程 ZIP 接受入口。清单自带的二进制摘要不构成发布者信任。高级手动注册仍支持绝对路径、工具 ID 和可信二进制 SHA。

安装只保存单个自包含可执行文件和清单，不把浏览器脚本装进本体。更新创建新目录，不覆盖旧版；任务绑定其启动版本。多个插件按工具 ID 注册，用户明确切换活动插件；同一时刻一个采集或登录任务。插件具有本机程序权限，独立进程不是安全沙箱，只安装可信发布者提供的插件。

注册时显式选择协议 1 或 2，不通过失败重试猜版本。每次调用都核验已配置程序摘要，再用该版本 `identity` 核对身份和能力。工具版本、采集协议版本、本地处理协议及云端 wire 版本彼此独立。

### v2 请求与终态

继续使用参数数组 `[absolute_executable_path, "protocol"]`、一次进程一个请求，stdin 一行 UTF-8 JSON 后关闭，stdout 有界 NDJSON。请求信封为 `{"protocol_version":2,"method":"...","params":{...}}`。不得用 CLI 人类文本替代协议；不得返回网页 HTML、任意脚本或可执行 UI。

| 方法 | params | 终态 |
|---|---|---|
| `identity` | `{}` | `type: identity`，`tool_id,tool_version,protocol_version,capabilities` |
| `auth_status` | `{}` | `type: auth`，`status: signed_out 或 signed_in` |
| `auth_login` | `{}` | 同 auth_status；插件自己的登录窗口完成认证后退出 |
| `auth_logout` | `{}` | 同 auth_status；清除插件持有的登录状态及计划 |
| `catalog` | `{parent_id:null或ID,cursor:null或ID,query:字符串}` | `type: catalog`，`items:[{id,title,kind:collection或resource}]`、`next_cursor:null或ID` |
| `plan` | `{resource_id:ID}` | `type: plan`，`plan_id,title,lecture_date,media_hosts,expires_at` |
| `collect` | `{plan_id:ID,allowed_media_hosts:[域名],output_path:新绝对路径}` | 原有 `progress` 阶段与 `result/output_path` |

能力声明只列实际支持的方法。纯本地转换继续使用 v1；不支持登录的 v2 资源插件不应伪造登录流程。桌面根据声明展示可用操作，不根据站点 ID 分支。

目录页最多 100 项，整个帧仍最多 16 KiB；插件必须按实际 UTF-8 编码长度分页，不能丢弃溢出条目。ID、游标和计划 ID 最长 256 个 ASCII 字符，只允许字母、数字、`-_.:`，不得编码凭证或 URL。显示标题最长 300 字符，可包含普通问号和 @ 标点，但不得含控制字符、URL 或 Cookie、Authorization、auth_key、token、signature 等敏感形态。未知授课日期为 null。

`plan` 可联网读取必要的元数据，但不得下载媒体。`media_hosts` 为 1–16 个精确小写域名，覆盖本次所有视频和课件下载；`expires_at` 是 Unix 秒，须在未来且不超过 24 小时。用户看到资源和域名并确认后才能执行 `collect`。宿主保留一个内存计划，绑定插件路径、摘要、身份和域名；切换插件、查询或更改登录状态、开始执行及重启都会使该确认失效，需重新计划。

插件同时验证计划与当前会话、资源身份和有效期。执行时刷新短期媒体签名不能改变获准资源或扩大域名；变化则返回固定错误码，重新预检确认。输出路径由宿主分配，必须尚不存在；完整原子发布后才返回成功。下载的 HTTPS 验证、连接 IP 固定和拒绝媒体跳转保持不变。

### 登录、取消和材料边界

登录窗口属于独立插件进程；本体不接收密码、Cookie 或签名地址。登录必须在此请求内完成并关闭窗口，不能留下脱离宿主管理的浏览器服务。会话放入系统凭证库；插件不得用明文文件作为凭证库失效时的替代。可恢复的目录缓存和计划不保存签名 URL。

宿主将登录及采集显示为可取消任务，沿用 macOS 进程组与 Windows Job Object 管理全部后代。关闭登录窗口、过期会话、过期计划、权限不足与下载失败使用稳定错误码，不返回异常原文。stderr 不进入 GUI 或日志。

采集成功仍需完整 `local_inspect`，其后复用 `local_start`、材料核对和 `prepare`。只有用户另行确认才 `submit`；标准媒体 ZIP、私有源包和整个工作区不得作为云端上传材料。平台字幕必须保留来源与真实时间单位，不能冒充本机 ASR，也不能按课件序号推测视频时间。

### 开发者与发行验收

用不含站点名的合成插件验证目录、分页、确认、取消和标准包输出，证明新站点无需修改宿主。智云适配器独立测试登录 API、回放和课件映射、域名变更、签名失效、无权限与原子输出。真实平台登录、原生 UI、Windows 子进程树与真实课程全链路分别验收，不能由合成结果替代。

插件源码、依赖和 NOTICE 独立维护；第三方版权声明随插件发行。公开客户端 wheel、冻结 sidecar 和桌面安装包按实际清单核验，排除站点适配代码、登录 WebView 依赖和浏览器脚本。

## 2026-09-28 GUI 工作流

普通新建首先提供标准资源包/音视频/UTF-8 转写。“从浏览器导出包导入”是独立次入口；未配置时引导到设置核对已安装插件的身份、绝对路径和可信 SHA，成功后回到同一个创建草稿。采集控制器跨页保留，任务页可查看真实进度和取消；完成的标准包需显式点击导入，再执行完整 local_inspect，不用历史采集完成回调覆盖新材料。

只变更 GUI 布局与状态归属。以下 protocol 1 字段、独立发行、每次身份/摘要核验、媒体域名明确确认、源 SHA 绑定、原件保护和云端单独同意要求全部保留。插件注册和采集均不授权上传。

Course Compiler 的采集插件是独立安装的本机程序。开发者可以用任何语言实现以下接口；智云插件是第一个实现，并非 GUI 允许的唯一站点。当前客户端保存一个活动插件配置。插件市场可以在此协议上提供目录、发布签名和安装器；当前实现不依赖在线市场服务。

## 注册与信任

用户明确选择已安装的可执行文件，提供 `tool_id` 和可信渠道的 SHA-256。ID 使用 1–80 个小写 ASCII 字母、数字、点、下划线或连字符，首字符必须是字母或数字。桌面核对可执行文件摘要后才调用 `identity`；每次使用再次核对摘要和身份。身份自报不能替代发布者信任，ZIP 不能指定程序、摘要或更新地址。开发者应发布自包含原生 executable，避免未被该摘要覆盖的可变脚本/动态依赖。macOS 和 Windows 分别构建、测试及发布摘要。

独立发布清单示例（用于发布渠道和人工注册，不从课程包读取）：

```json
{
  "kind": "independent-collector-plugin",
  "tool_id": "org.example.lecture-collector",
  "protocol_version": 1,
  "tool_version": "1.0.0",
  "platform": "windows-x64",
  "executable": "lecture-collector.exe",
  "sha256": "发布者计算的64位十六进制摘要"
}
```

## 传输

桌面通过参数数组 `[absolute_executable_path, "protocol"]` 启动一次进程处理一次请求。stdin 写入一行 UTF-8 JSON 并关闭；stdout 只允许 UTF-8 NDJSON。不得在 stdout 写 banner、日志、堆栈、URL 或 Cookie。stderr 被丢弃，诊断应映射为稳定错误码。不得要求交互式终端或在后台脱离父进程运行。

请求信封只有 `protocol_version`（整数 1）、`method`、`params`。字段集合严格校验，未知方法、字段或版本必须失败。客户端每帧最大 16 KiB，最多 1000 个进度帧；开发者应只在阶段变化时发进度。版本升级必须显式协商，不能通过课程材料改变接口。

### identity

请求：`{"protocol_version":1,"method":"identity","params":{}}`

响应：

```json
{"type":"identity","tool_id":"org.example.lecture-collector","protocol_version":1,"tool_version":"1.0.0","capabilities":["inspect","convert"]}
```

### inspect

请求 `params`：`{"source_path":"/absolute/browser-export.zip"}`。

响应：

```json
{"type":"inspection","tool_id":"org.example.lecture-collector","protocol_version":1,"tool_version":"1.0.0","capabilities":["inspect","convert"],"course_title":"示例课程","lecture_date":null,"media_hosts":["media.example.org"],"source_sha256":"64位小写十六进制摘要"}
```

预检不能下载媒体或联网探测。域名只允许小写 ASCII DNS 名（IDN 转 ASCII），不得含 scheme、端口、路径、查询、片段或凭证；日期未知为 null。标题最多 300 字符，不得用标题等显示字段夹带 URL 或凭证。摘要必须对应实际读取的同一份输入快照。GUI 只提取白名单显示字段，用户确认前不能启动转换。

`0.3.0.dev2` 可选增加 `source_id`（1–120 个 ASCII 字母、数字、下划线或连字符）及 `source_fingerprint`（64 位小写十六进制），用于旧 TUI 的稳定讲次身份和材料缓存。它们由插件计算，不含原始 URL、Cookie 或签名；媒体签名轮换不改变相同资源的指纹，资源或课件变化会改变指纹。未提供此扩展的其他网站插件仍可使用原有 `source_sha256` 通用身份。请求字段、协议版本和 `convert` 的源快照校验不变；旧 GUI 仅投影其已知字段。

### convert

请求 `params`：`source_path,source_sha256,allowed_media_hosts,output_path`；不接受软件路径、任意命令、TLS 绕过开关或回调 URL。必须重新验证源摘要和精确已确认域名集合，源已改变或域名不一致即失败。输出只能写入新的 `output_path`；不得覆盖原件或已有文件。

可选阶段帧：`{"type":"progress","stage":"preparing"}`。阶段只允许 `preparing,downloading,packaging,publishing`。没有可靠总量时不声称百分比。下载只访问确认范围，不能用重定向扩展授权；实现者负责正常 HTTPS 证书校验等网络边界。

成功终态：`{"type":"result","output_path":"/absolute/new-standard.zip"}`，随后以退出码 0 结束。必须先完整关闭并原子发布标准包，再发送成功帧。包遵循 [本地资产规范](STANDARD_PACKAGE.md)，只含已落地资源，不含原始私有包、URL、Cookie、程序和采集配置。桌面还会运行完整 `local_inspect`，协议成功不等于导入成功。

失败终态：`{"type":"error","code":"COLLECTOR_FAILED","message":"采集未完成，请检查授权后重试"}`，随后非零退出。不能输出异常原文；桌面只显示自己的固定脱敏提示。失败或中断不得发布半成品，不得删除源文件。客户端可终止整个进程树；插件应在输出目录中暂存，支持中断后清理，不在用户其他目录遗留私有副本。

## 开发与发行验证

智云参考实现位于 `src/handouter_zhiyun_collector/protocol.py`。`tests/test_collector_protocol.py` 使用合成导出包和假媒体服务验证无下载预检、来源绑定、域名确认、脱敏及原子输出；桌面 `collector.rs` 与前端插件测试验证外部接口和确认行为。其它插件应运行相同正向/负向矩阵，并在目标平台测试取消及全部后代终止。

公共客户端只携带通用宿主，不内嵌具体采集器。插件自身可含站点解析和浏览器脚本，但不得携带私有写作/云生成规则。注册插件及确认本机下载均不授权云上传；后续上传保持独立确认。
