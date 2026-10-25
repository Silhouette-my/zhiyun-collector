"""Exercise the browser exporter's DOM fallback and fetch body deadline offline."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "src/handouter_zhiyun_collector/zhiyun_exporter.user.js"


@unittest.skipUnless(shutil.which("node"), "node required")
class ExporterDOMTimingTests(unittest.TestCase):
    def test_dom_images_without_timestamp_stay_unknown(self):
        source = SCRIPT.read_text(encoding="utf-8")
        helpers = source[source.index("    function timeStrToSeconds"):source.index("    function saveBlobToFile")]
        probe = """
const MAX_VUE_SCAN_ELEMENTS = 3000;
const NodeFilter = {SHOW_ELEMENT: 1};
const document = {
    title: 'Synthetic course', body: {}, querySelector: () => null,
    createTreeWalker: () => ({currentNode: null}),
    querySelectorAll: selector => selector.startsWith('.ppt_container img')
        ? [{src: 'https://example.invalid/slide.png', getAttribute: () => null}] : []
};
const slide = getBestPptData().pptList[0];
console.log(JSON.stringify(extractPptTiming(slide)));
"""
        run = subprocess.run([shutil.which("node"), "-e", helpers + probe],
                             check=True, capture_output=True, text=True, timeout=5)
        value = json.loads(run.stdout)
        self.assertIsNone(value["seconds"])
        self.assertIsNone(value["timestamp"])

    def test_fetch_deadline_includes_response_body(self):
        source = SCRIPT.read_text(encoding="utf-8")
        helper = source[source.index("    async function fetchImageBlob"):source.index("    // 并发控制器")]
        probe = """
global.fetch = async (_url, options) => ({ok: true, blob: () => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(Error('synthetic stalled body')));
})});
global.Image = class {set src(value) {if (value) queueMicrotask(() => this.onerror());}};
fetchImageBlob('https://example.invalid/slide.png', 20).then(value => {
    if (value !== null) throw Error('unexpected blob');
    console.log('body deadline passed');
});
"""
        run = subprocess.run([shutil.which("node"), "-e", helper + probe],
                             check=True, capture_output=True, text=True, timeout=5)
        self.assertIn("body deadline passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
