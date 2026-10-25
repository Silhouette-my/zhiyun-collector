// ==UserScript==
// @name         浙大智云课堂全自动讲义资产打包导出器 (ZJU Zhiyun Lecture Exporter)
// @namespace    https://github.com/michaeltan/zju-zhiyun-exporter
// @version      1.4.2
// @description  导出智云课堂 PPT、已知切换时间与私有媒体来源为资产 ZIP，交给 Course Compiler 导入；讲义生成由后续 Agent 任务完成。
// @author       Antigravity
// @match        *://interactivemeta.cmc.zju.edu.cn/*
// @match        *://classroom.zju.edu.cn/*
// @match        *://livingroom.cmc.zju.edu.cn/*
// @match        *://onlineroom.cmc.zju.edu.cn/*
// @match        *://*.cmc.zju.edu.cn/*
// @grant        none
// @require      https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    console.log('[ZhiyunExporter v1.4.2] 启动中...');

    // Browser safety limits. Asset export runs inside the lecture page's renderer;
    // keep DOM traversal and image memory bounded so a large course cannot starve Chrome.
    const MAX_VUE_SCAN_ELEMENTS = 3000;
    const PPT_DOWNLOAD_CONCURRENCY = 3;
    const MAX_SLIDES_PER_EXPORT = 400;
    const MAX_ARCHIVE_SOURCE_BYTES = 256 * 1024 * 1024;
    const MAX_CANVAS_DIMENSION = 4096;
    const MAX_CANVAS_PIXELS = 16 * 1024 * 1024;

    function timeStrToSeconds(str) {
        if (typeof str !== 'string' || !str.trim()) return null;
        const parts = str.trim().split(':');
        if (parts.length !== 2 && parts.length !== 3) return null;
        const values = parts.map(Number);
        if (values.some((v) => !Number.isFinite(v) || v < 0)) return null;
        const seconds = values[values.length - 1];
        if (seconds >= 60) return null;
        if (parts.length === 3) {
            if (values[1] >= 60) return null;
            return values[0] * 3600 + values[1] * 60 + seconds;
        }
        return values[0] * 60 + seconds;
    }

    function secToTime(sec) {
        if (typeof sec !== 'number' || !Number.isFinite(sec) || sec < 0) return null;
        const totalMs = Math.round(sec * 1000);
        const wholeSeconds = Math.floor(totalMs / 1000);
        const millis = totalMs % 1000;
        const h = Math.floor(wholeSeconds / 3600);
        const m = Math.floor((wholeSeconds % 3600) / 60);
        const s = wholeSeconds % 60;
        const suffix = millis ? `.${String(millis).padStart(3, '0').replace(/0+$/, '')}` : '';
        return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}${suffix}`;
    }

    function getFormattedDate() {
        const d = new Date();
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}`;
    }

    function parseLectureDate(value) {
        if (typeof value !== 'string') return null;
        const text = value.trim();
        const match = text.match(/(?:^|[^0-9])(20[0-9]{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12][0-9]|3[01])(?:日|[^0-9]|$)/);
        if (!match) return null;
        const year = Number(match[1]);
        const month = Number(match[2]);
        const day = Number(match[3]);
        const date = new Date(Date.UTC(year, month - 1, day));
        if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
        return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    }

    function extractLectureDate(value) {
        if (!value || typeof value !== 'object') return null;
        const preferred = ['lectureDate', 'lecture_date', 'classDate', 'class_date', 'courseDate', 'course_date',
            'sub_title', 'subTitle', 'startDate', 'start_date'];
        const queue = [{ value, depth: 0 }];
        const seen = new Set();
        let visited = 0;
        while (queue.length && visited < 500) {
            const current = queue.shift();
            const item = current.value;
            if (!item || typeof item !== 'object' || seen.has(item)) continue;
            seen.add(item);
            visited++;
            for (const key of preferred) {
                if (Object.prototype.hasOwnProperty.call(item, key)) {
                    const found = parseLectureDate(item[key]);
                    if (found) return found;
                }
            }
            if (current.depth < 3) {
                for (const child of Object.values(item)) {
                    if (child && typeof child === 'object') queue.push({ value: child, depth: current.depth + 1 });
                }
            }
        }
        return null;
    }

    // 智能多字段提取图片直链
    function extractPptUrl(item) {
        if (!item) return '';
        if (typeof item === 'string') {
            const trimmed = item.trim();
            if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
                try {
                    const parsed = JSON.parse(trimmed);
                    return extractPptUrl(parsed);
                } catch (e) {}
            }
            if (item.startsWith('http') || item.startsWith('//') || item.includes('.jpg') || item.includes('.png') || item.includes('.webp') || item.includes('cmc.zju.edu.cn')) {
                return item;
            }
            return '';
        }
        if (typeof item === 'object') {
            const keys = ['pptimgurl', 'pptthumb', 'imgSrc', 'img_src', 'picUrl', 'pic_url', 'url', 'pic', 'imageUrl', 'image_url', 'src', 'img', 'thumbnail', 'path', 'originUrl', 'snapshot'];
            for (const k of keys) {
                if (item[k] && typeof item[k] === 'string' && item[k].length > 5) return item[k];
            }
            // 深度查找任意字符串属性包含图片路径
            for (const v of Object.values(item)) {
                if (typeof v === 'string') {
                    const sub = extractPptUrl(v);
                    if (sub) return sub;
                }
            }
        }
        return '';
    }

    // 时间字段必须有明确单位。字符串时钟可直接解析；平台的 `created`
    // 已知按秒使用。其他数值字段不再通过 >10000 之类的阈值猜单位，
    // 而是保留原值并标记 unknown_numeric_unit，等待真实页面契约确认。
    function extractPptTiming(item) {
        const unknown = (field = null, rawValue = null, status = 'unknown') => ({
            timestamp: null,
            seconds: null,
            timeStatus: status,
            sourceField: field,
            rawValue: rawValue,
        });
        if (!item) return unknown();
        if (typeof item === 'string') {
            const trimmed = item.trim();
            if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
                try {
                    return extractPptTiming(JSON.parse(trimmed));
                } catch (e) {
                    return unknown(null, trimmed, 'invalid_json');
                }
            }
            const seconds = timeStrToSeconds(trimmed);
            if (seconds !== null) {
                return { timestamp: secToTime(seconds), seconds, timeStatus: 'clock_string', sourceField: null, rawValue: trimmed };
            }
            return unknown(null, trimmed, 'unrecognized_string');
        }
        if (typeof item !== 'object') return unknown();

        if (typeof item.created === 'number' && Number.isFinite(item.created) && item.created >= 0) {
            return {
                timestamp: secToTime(item.created),
                seconds: item.created,
                timeStatus: 'known_seconds',
                sourceField: 'created',
                rawValue: item.created,
            };
        }

        const keys = ['switchTime', 'switch_time', 'time', 'markTime', 'mark_time', 'playTime', 'play_time', 'timeInVideo', 'pts'];
        for (const k of keys) {
            if (typeof item[k] === 'string') {
                const seconds = timeStrToSeconds(item[k]);
                if (seconds !== null) {
                    return { timestamp: secToTime(seconds), seconds, timeStatus: 'clock_string', sourceField: k, rawValue: item[k] };
                }
            }
        }
        for (const k of keys) {
            if (typeof item[k] === 'number' && Number.isFinite(item[k]) && item[k] >= 0) {
                return unknown(k, item[k], 'unknown_numeric_unit');
            }
        }
        for (const [k, v] of Object.entries(item)) {
            if (typeof v === 'string') {
                const seconds = timeStrToSeconds(v);
                if (seconds !== null) {
                    return { timestamp: secToTime(seconds), seconds, timeStatus: 'clock_string', sourceField: k, rawValue: v };
                }
            }
        }
        return unknown();
    }

    function extractPptTime(item) {
        return extractPptTiming(item).timestamp;
    }

    function isUsableMediaUrl(url) {
        if (!url || typeof url !== 'string') return false;
        const clean = url.trim();
        if (!clean.startsWith('http://') && !clean.startsWith('https://')) return false;
        if (clean.includes('95c332f44fd741f5ac6d1643252dd9f2')) return false;
        return true;
    }

    // 智能扫描最优 PPT 列表
    function getBestPptData() {
        let pptList = [];
        let courseName = document.title.split('-')[0].trim();
        let teacherName = '任课教师';
        let videoUrl = '';
        let lectureDate = null;

        try {
            // 1. 优先检查典型根容器中的真实视频地址（v.videoUrl / v.liveUrl / v.playUrl）
            const primarySelectors = ['.living-page-wrapper', '.course-info__main', '#cmcPlayer_container', '#app', '.ppt_container', '#playWrapper'];
            for (const sel of primarySelectors) {
                const el = document.querySelector(sel);
                if (el && el.__vue__) {
                    const v = el.__vue__;
                    if (v.courseName && courseName === '智云课程') courseName = v.courseName;
                    if (v.teacherName) teacherName = v.teacherName;
                    if (!lectureDate) lectureDate = extractLectureDate(v);
                    if (!videoUrl && isUsableMediaUrl(v.videoUrl)) videoUrl = v.videoUrl;
                    if (!videoUrl && isUsableMediaUrl(v.liveUrl)) videoUrl = v.liveUrl;
                    if (!videoUrl && isUsableMediaUrl(v.playUrl)) videoUrl = v.playUrl;

                    for (const prop of ['pptList', 'slideList', 'slides', 'pictureList']) {
                        if (Array.isArray(v[prop]) && v[prop].length > 0) {
                            if (extractPptUrl(v[prop][0])) {
                                pptList = v[prop];
                                break;
                            }
                        }
                    }
                }
                if (pptList.length > 0 && videoUrl) break;
            }

            // 2. 有界遍历 Vue 实例。旧实现 querySelectorAll('*') 会一次性物化整页
            // NodeList，在复杂回放页上会制造明显的 renderer CPU/内存峰值。TreeWalker
            // 按需前进并设置硬上限；超过范围时宁可要求展开 PPT 栏，也不扫完整页面。
            if (pptList.length === 0 || !videoUrl) {
                let candidateList = [];
                let scanned = 0;
                const walker = document.createTreeWalker(
                    document.body || document.documentElement,
                    NodeFilter.SHOW_ELEMENT
                );
                let el = walker.currentNode;
                while (el && scanned < MAX_VUE_SCAN_ELEMENTS) {
                    scanned++;
                    const v = el.__vue__;
                    if (v) {
                        if (v.courseName && !courseName) courseName = v.courseName;
                        if (!lectureDate) lectureDate = extractLectureDate(v);
                        if (!videoUrl && isUsableMediaUrl(v.videoUrl)) videoUrl = v.videoUrl;
                        if (!videoUrl && isUsableMediaUrl(v.liveUrl)) videoUrl = v.liveUrl;
                        if (!videoUrl && isUsableMediaUrl(v.playUrl)) videoUrl = v.playUrl;

                        for (const prop of ['pptList', 'slideList', 'slides', 'pictureList']) {
                            if (Array.isArray(v[prop]) && v[prop].length > 0) {
                                const url = extractPptUrl(v[prop][0]);
                                if (url) {
                                    pptList = v[prop];
                                    break;
                                }
                                if (v[prop].length > candidateList.length) candidateList = v[prop];
                            }
                        }
                    }
                    if (pptList.length > 0 && videoUrl) break;
                    el = walker.nextNode();
                }
                if (pptList.length === 0 && candidateList.length > 0) pptList = candidateList;
            }

            // 3. 日期兜底：智云讲次标题常含 YYYY-MM-DD；只读取明确的标题节点，不使用导出时间。
            if (!lectureDate) {
                const titleNodes = document.querySelectorAll('.sub-title, .living-title, .course-info__title, h1, h2');
                for (const node of titleNodes) {
                    lectureDate = parseLectureDate(node.textContent || '');
                    if (lectureDate) break;
                }
            }

            // 4. 兜底方案：从 DOM video 标签中抓取真实地址（避开静态占位视频与 blob:）
            if (!videoUrl) {
                const video = document.querySelector('#cmc_player_video') || document.querySelector('video');
                if (video && isUsableMediaUrl(video.src)) videoUrl = video.src;
            }

            // 5. 兜底方案：直接从 DOM 图片标签中抓取
            if (pptList.length === 0) {
                const domImages = document.querySelectorAll('.ppt_container img, .ppt-list img, img[src*="cmc.zju.edu.cn"], img[src*="ppt"]');
                if (domImages.length > 0) {
                    pptList = Array.from(domImages).map((img) => ({
                        imgSrc: img.src,
                        switchTime: img.getAttribute('data-time') || null
                    }));
                }
            }
        } catch (err) {
            console.error('[ZhiyunExporter] 扫描数据出错:', err);
        }

        return {
            pptList: pptList || [],
            courseName: courseName || '智云课程',
            teacherName: teacherName || '教师',
            lectureDate: lectureDate || null,
            videoUrl: videoUrl || ''
        };
    }

    function saveBlobToFile(blob, filename) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            a.remove();
            URL.revokeObjectURL(url);
        }, 1500);
    }

    // 优先保留来源图片；必要时用有界 Canvas 转换。
    async function fetchImageBlob(rawUrl, timeoutMs = 10000) {
        if (!rawUrl || typeof rawUrl !== 'string' || rawUrl.trim() === '') {
            return null;
        }
        let cleanUrl = rawUrl.trim();
        if (cleanUrl.startsWith('//')) cleanUrl = 'https:' + cleanUrl;
        else if (cleanUrl.startsWith('http://')) cleanUrl = 'https://' + cleanUrl.slice(7);

        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);

        try {
            const resp = await fetch(cleanUrl, {
                signal: controller.signal,
                mode: 'cors',
                headers: { 'Accept': 'image/*,*/*' }
            });
            if (resp.ok) {
                const b = await resp.blob();
                if (b && b.size > 2000) return b;
            }
        } catch (e) {
            // Failed or timed-out requests may still use the existing image fallback.
        } finally {
            clearTimeout(timer);
        }

        // 备选降级方案：创建带 crossOrigin 的 Image 走 Canvas 导出
        return new Promise((resolve) => {
            const img = new Image();
            img.crossOrigin = 'anonymous';
            const failTimer = setTimeout(() => {
                img.src = '';
                resolve(null);
            }, timeoutMs);

            img.onload = () => {
                clearTimeout(failTimer);
                let canvas = null;
                try {
                    const sourceWidth = img.naturalWidth || 1920;
                    const sourceHeight = img.naturalHeight || 1080;
                    const dimensionScale = Math.min(1, MAX_CANVAS_DIMENSION / sourceWidth, MAX_CANVAS_DIMENSION / sourceHeight);
                    const pixelScale = Math.min(1, Math.sqrt(MAX_CANVAS_PIXELS / (sourceWidth * sourceHeight)));
                    const scale = Math.min(dimensionScale, pixelScale);
                    const width = Math.max(1, Math.round(sourceWidth * scale));
                    const height = Math.max(1, Math.round(sourceHeight * scale));
                    canvas = document.createElement('canvas');
                    canvas.width = width;
                    canvas.height = height;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0, width, height);
                    canvas.toBlob((b) => {
                        canvas.width = 0;
                        canvas.height = 0;
                        img.onload = null;
                        img.onerror = null;
                        img.src = '';
                        if (b && b.size > 2000) resolve(b);
                        else resolve(null);
                    }, 'image/jpeg', 0.90);
                } catch (canvasErr) {
                    if (canvas) {
                        canvas.width = 0;
                        canvas.height = 0;
                    }
                    img.onload = null;
                    img.onerror = null;
                    img.src = '';
                    resolve(null);
                }
            };
            img.onerror = () => {
                clearTimeout(failTimer);
                resolve(null);
            };
            img.src = cleanUrl;
        });
    }

    // 并发控制器
    async function runConcurrentTasks(items, limit, taskFn, onProgress) {
        const results = new Array(items.length);
        let currentIndex = 0;
        let completed = 0;

        async function worker() {
            while (currentIndex < items.length) {
                const idx = currentIndex++;
                try {
                    results[idx] = await taskFn(items[idx], idx);
                } catch (err) {
                    results[idx] = null;
                }
                completed++;
                if (onProgress) onProgress(completed, items.length);
            }
        }

        const workers = [];
        for (let i = 0; i < Math.min(limit, items.length); i++) {
            workers.push(worker());
        }
        await Promise.all(workers);
        return results;
    }

    function createExporterUI() {
        if (document.getElementById('zhiyun-exporter-panel')) return;

        const panel = document.createElement('div');
        panel.id = 'zhiyun-exporter-panel';
        panel.style.cssText = `
            position: fixed;
            top: 75px;
            right: 20px;
            z-index: 2147483647;
            background: rgba(15, 23, 42, 0.96);
            color: #f8fafc;
            border: 1px solid rgba(56, 189, 248, 0.4);
            border-radius: 12px;
            padding: 14px 18px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(12px);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 13px;
            width: 270px;
            user-select: none;
            transition: all 0.3s ease;
        `;

        panel.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 8px;">
                <span style="font-weight: bold; font-size: 14px; color: #38bdf8; display: flex; align-items: center; gap: 6px;">
                    <span>🎓</span> 智云课堂讲义导出器
                </span>
                <span id="zy-close-btn" style="cursor: pointer; color: #94a3b8; font-size: 18px; line-height: 1;">×</span>
            </div>
            <div id="zy-status" style="margin-bottom: 12px; color: #94a3b8; font-size: 12px; min-height: 18px; line-height: 1.4;">
                就绪
            </div>
            <div style="display: flex; flex-direction: column; gap: 8px;">
                <button id="zy-export-zip-btn" style="background: linear-gradient(135deg, #0284c7, #2563eb); color: #fff; border: none; border-radius: 6px; padding: 9px 12px; font-weight: 600; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; box-shadow: 0 4px 12px rgba(37,99,235,0.3);">
                    <span>⚡ 一键打包资产包 (.zip)</span>
                </button>
                <div style="display: flex; gap: 6px;">
                    <button id="zy-copy-ppt-btn" style="flex: 1; background: rgba(255,255,255,0.08); color: #e2e8f0; border: 1px solid rgba(255,255,255,0.15); border-radius: 6px; padding: 7px 8px; font-size: 12px; cursor: pointer;">
                        📋 复制 PPT 数据
                    </button>
                    <button id="zy-copy-video-btn" style="flex: 1; background: rgba(255,255,255,0.08); color: #e2e8f0; border: 1px solid rgba(255,255,255,0.15); border-radius: 6px; padding: 7px 8px; font-size: 12px; cursor: pointer;">
                        🎬 复制视频直链
                    </button>
                </div>
            </div>
            <div style="margin-top: 10px; font-size: 11px; color: #64748b; line-height: 1.4;">
                导出包保留可验证的 PPT 时间证据；未知单位不会猜测为秒/毫秒。配合 Course Compiler 本地整理后交给你的 Agent。
            </div>
        `;

        document.body.appendChild(panel);

        const statusEl = panel.querySelector('#zy-status');
        const exportZipBtn = panel.querySelector('#zy-export-zip-btn');
        const copyPptBtn = panel.querySelector('#zy-copy-ppt-btn');
        const copyVideoBtn = panel.querySelector('#zy-copy-video-btn');
        const closeBtn = panel.querySelector('#zy-close-btn');

        closeBtn.onclick = () => {
            panel.style.display = 'none';
        };

        function updateReadyStatus() {
            const data = getBestPptData();
            if (data.pptList && data.pptList.length > 0) {
                const sampleUrl = extractPptUrl(data.pptList[0]);
                if (sampleUrl) {
                    statusEl.textContent = `✅ 识别到《${data.courseName}》(有效 PPT ${data.pptList.length} 页)`;
                } else {
                    statusEl.textContent = `⚠️ 检测到 ${data.pptList.length} 页项，请先点右侧「<」展开 PPT 列表。`;
                }
            } else {
                statusEl.textContent = '就绪。如未检测到 PPT，请展开右侧栏。';
            }
        }
        updateReadyStatus();

        // 复制 PPT JSON 数据
        copyPptBtn.onclick = async () => {
            const data = getBestPptData();
            if (!data.pptList || data.pptList.length === 0) {
                statusEl.textContent = '❌ 未检测到 PPT 列表，请点击右侧「<」展开栏后再试。';
                return;
            }
            const res = data.pptList.map((p, i) => {
                const url = extractPptUrl(p);
                const timing = extractPptTiming(p);
                return {
                    index: i + 1,
                    timestamp: timing.timestamp,
                    seconds: timing.seconds,
                    timeStatus: timing.timeStatus,
                    sourceField: timing.sourceField,
                    rawTimeValue: timing.rawValue,
                    url: url
                };
            });
            const jsonText = JSON.stringify(res, null, 2);
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(jsonText);
            } else {
                const ta = document.createElement('textarea');
                ta.value = jsonText;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                ta.remove();
            }
            statusEl.textContent = `✅ 成功复制 ${res.length} 页 PPT 数据至剪贴板！`;
        };

        // 复制视频直链
        copyVideoBtn.onclick = async () => {
            const data = getBestPptData();
            const fallbackVideo = document.querySelector('video')?.src || '';
            const url = data.videoUrl || (isUsableMediaUrl(fallbackVideo) ? fallbackVideo : '');
            if (!url) {
                statusEl.textContent = '❌ 未找到有效视频直链，请先播放视频。';
                return;
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(url);
            }
            statusEl.textContent = '✅ 已成功将视频直链复制到剪贴板！';
        };

        // 一键打包 ZIP (并发多线程高速下载，杜绝空文件)
        exportZipBtn.onclick = async () => {
            const data = getBestPptData();
            if (!data.pptList || data.pptList.length === 0) {
                statusEl.textContent = '❌ 未定位到 PPT 数据，请展开右侧 PPT 栏。';
                return;
            }

            exportZipBtn.disabled = true;
            exportZipBtn.style.opacity = '0.6';

            try {
                if (typeof JSZip === 'undefined') {
                    throw new Error('JSZip 库加载中，请稍候点击');
                }

                const total = data.pptList.length;
                if (total > MAX_SLIDES_PER_EXPORT) {
                    throw new Error(`PPT 共 ${total} 页，超过单次安全上限 ${MAX_SLIDES_PER_EXPORT} 页；请分课程/讲次导出，避免浏览器内存峰值过高。`);
                }
                statusEl.textContent = `[*] 正在抓取 ${total} 页 PPT 高清图（低内存模式）...`;

                const zip = new JSZip();
                const slidesFolder = zip.folder('slides');
                const metaList = [];

                // 准备任务
                const tasks = data.pptList.map((ppt, i) => {
                    const url = extractPptUrl(ppt);
                    const timing = extractPptTiming(ppt);
                    return {
                        index: i + 1,
                        url: url,
                        timing: timing,
                        filename: `slide_${String(i + 1).padStart(3, '0')}.jpg`
                    };
                });

                let validImageCount = 0;
                let totalImageBytes = 0;
                let archiveLimitExceeded = false;

                // 降低并发，避免 fetch Blob + 图片解码 + JSZip 同时推高 renderer 内存。
                await runConcurrentTasks(tasks, PPT_DOWNLOAD_CONCURRENCY, async (task) => {
                    let downloaded = false;
                    if (archiveLimitExceeded) return;
                    if (task.url) {
                        const blob = await fetchImageBlob(task.url);
                        if (blob && blob.size > 2000) {
                            if (totalImageBytes + blob.size > MAX_ARCHIVE_SOURCE_BYTES) {
                                archiveLimitExceeded = true;
                            } else {
                                totalImageBytes += blob.size;
                                // JPEG 已压缩，再 DEFLATE 收益很小却增加 CPU/内存；直接 STORE。
                                slidesFolder.file(task.filename, blob, { compression: 'STORE' });
                                validImageCount++;
                                downloaded = true;
                            }
                        }
                    }
                    metaList.push({
                        index: task.index,
                        filename: task.filename,
                        timestamp: task.timing.timestamp,
                        seconds: task.timing.seconds,
                        timeStatus: task.timing.timeStatus,
                        sourceField: task.timing.sourceField,
                        rawTimeValue: task.timing.rawValue,
                        status: downloaded ? 'ok' : 'missing_image'
                    });
                }, (done, totalCount) => {
                    statusEl.textContent = `[*] 正在极速拉取 PPT [${done}/${totalCount}] (有效: ${validImageCount})...`;
                });

                if (archiveLimitExceeded) {
                    throw new Error(`PPT 图片累计超过 ${Math.round(MAX_ARCHIVE_SOURCE_BYTES / 1024 / 1024)} MiB 安全上限；已停止继续装入内存。请分批导出或减少单次课件页数。`);
                }

                metaList.sort((a, b) => a.index - b.index);
                zip.file('slides_meta.json', JSON.stringify(metaList, null, 2));

                const courseInfo = {
                    schemaVersion: 2,
                    courseName: data.courseName || '智云课程',
                    teacherName: data.teacherName || '任课教师',
                    lectureDate: data.lectureDate || null,
                    exportDate: new Date().toISOString(),
                    totalSlides: total,
                    validImages: validImageCount,
                    missingImages: total - validImageCount,
                    unknownSlideTimes: metaList.filter((item) => item.seconds === null).length
                };
                zip.file('course_info.json', JSON.stringify(courseInfo, null, 2));

                // 签名媒体 URL 与具体回放页只放 private/，Handouter 的 Agent
                // 交接流程不会导入该目录。资产包本身仍应按私有文件保存。
                const fallbackVideo = document.querySelector('video')?.src || '';
                const privateSource = {
                    videoUrl: data.videoUrl || (isUsableMediaUrl(fallbackVideo) ? fallbackVideo : ''),
                    pageUrl: window.location.href
                };
                zip.folder('private').file('source.json', JSON.stringify(privateSource, null, 2));

                if (validImageCount === 0) {
                    throw new Error('未抓取到有效图片数据，请先点击右侧「<」小箭头展开 PPT 列表后再打包！');
                }

                statusEl.textContent = '📦 正在压缩生成资产包...';
                const zipContent = await zip.generateAsync({
                    type: 'blob',
                    compression: 'STORE',
                    streamFiles: true
                });

                const safeName = courseInfo.courseName.replace(/[\\/:*?"<>|]/g, '_');
                const zipFilename = `${safeName}_${getFormattedDate()}_资产包.zip`;

                saveBlobToFile(zipContent, zipFilename);
                const unknownTimes = metaList.filter((item) => item.seconds === null).length;
                statusEl.textContent = `🎉 打包完成：PPT ${validImageCount}/${total} 张，未知时间 ${unknownTimes} 条。资产包含 private/ 来源信息，请仅本地保存。`;
            } catch (err) {
                console.error(err);
                statusEl.textContent = `❌ ${err.message}`;
            } finally {
                exportZipBtn.disabled = false;
                exportZipBtn.style.opacity = '1';
            }
        };
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', createExporterUI);
    } else {
        createExporterUI();
    }
    setTimeout(createExporterUI, 1500);

})();
