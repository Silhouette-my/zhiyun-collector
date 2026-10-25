# 标准包输出

插件只发布新的 ZIP 路径，失败时不发布半成品、不覆盖原件。当前输出 schema 1：

```text
handouter-input.json
media/lecture.mp4        # 或已完成、未加密 HLS 的 lecture.ts
slides/slides_meta.json  # 可选
slides/images/slide_0001.jpg
```

清单包含 `schema_version: 1`、`lecture.course_title`、可空 `lecture.lecture_date`、`media.path` 和可选 `slides.images_dir/metadata_path`。课件事件保留 `filename/source_index/start_ms/end_ms`；未知时间为 null，不按页序推断。

`schemas/handouter-input-v1.schema.json` 将日期类型修正为字符串或 null，与既有插件和桌面本地运行时的未知日期契约一致；字段和 schema 版本不变。独立测试以本插件实际输出契约检查安全路径、成员引用、媒体/图片头及事件结构；本体完整 ZIP 安全、资源限额、可解码性和导入验收仍在 Course Compiler 执行。

禁止包含 `private/`、原始浏览器 ZIP、Cookie、签名 URL、可执行文件或采集配置。内容正文本身是不可信数据，不得作为命令或模型系统指令执行。标准媒体 ZIP 不是云端提交包；云端材料由本体白名单整理后单独确认。
