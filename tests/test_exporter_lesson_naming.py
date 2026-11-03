"""Offline checks for the userscript's lesson date / week / period naming."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "src/handouter_zhiyun_collector/zhiyun_exporter.user.js"

# Synthetic schedule: Tuesday 2026-09-15 08:00 and Thursday 2026-09-17 08:00, Asia/Shanghai.
TUE_0800 = 1789430400
THU_0800 = 1789603200


@unittest.skipUnless(shutil.which("node"), "node required")
class ExporterLessonNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = SCRIPT.read_text(encoding="utf-8")
        cls.helpers = source[source.index("    function timeStrToSeconds"):source.index("    function saveBlobToFile")]

    def _run(self, probe: str, tz: str = "America/Los_Angeles"):
        env = {**os.environ, "TZ": tz}
        run = subprocess.run([shutil.which("node"), "-e", self.helpers + probe], check=True,
                             capture_output=True, text=True, encoding="utf-8", timeout=10, env=env)
        return json.loads(run.stdout)

    def test_lesson_info_uses_shanghai_time_and_parses_periods(self):
        value = self._run(f"""
console.log(JSON.stringify(buildLessonInfo({{subTitle: '2026-09-17第1-2节', startAt: '{THU_0800}',
    endAt: '{THU_0800 + 5700}', termStartMonday: '2026-09-14', termStartBasis: 'first_lesson_week'}})));
""")
        self.assertEqual(value["date"], "2026-09-17")
        self.assertEqual(value["dateSource"], "start_at")
        self.assertEqual(value["weekday"], "周四")
        self.assertEqual(value["periods"], "1-2")
        self.assertEqual(value["startTime"], "0800")
        self.assertEqual(value["startAt"], "2026-09-17T08:00:00+08:00")
        self.assertEqual(value["endAt"], "2026-09-17T09:35:00+08:00")
        self.assertEqual(value["teachingWeek"], 1)
        self.assertEqual(value["teachingWeekBasis"], "first_lesson_week")

    def test_late_evening_utc_is_next_day_in_shanghai(self):
        # 2026-09-16T16:30Z is 00:30 on Thursday in Shanghai, whatever the machine timezone.
        for tz in ("America/Los_Angeles", "Asia/Shanghai", "UTC"):
            value = self._run(f"console.log(JSON.stringify(buildLessonInfo({{startAt: {THU_0800 - 7.5 * 3600:.0f}}})));", tz)
            self.assertEqual((value["date"], value["weekday"], value["startTime"]), ("2026-09-17", "周四", "0030"))

    def test_millisecond_or_garbage_start_is_not_guessed(self):
        value = self._run(f"""
console.log(JSON.stringify([buildLessonInfo({{startAt: '{THU_0800}000'}}).date,
    buildLessonInfo({{startAt: 'soon', subTitle: '2026-09-17第3节'}}),
    buildLessonInfo({{lectureDate: '2026-09-17'}}).dateSource]));
""")
        self.assertIsNone(value[0])
        self.assertEqual((value[1]["date"], value[1]["dateSource"], value[1]["periods"]), ("2026-09-17", "sub_title", "3"))
        self.assertEqual(value[2], "page")

    def test_term_start_is_monday_of_first_valid_lesson(self):
        value = self._run(f"""
const rows = [
  {{sub_id: '3', title: '2026-09-17第1-2节', start_at: '{THU_0800}', sub_delete: '0', sub_show: 'yes'}},
  {{sub_id: '1', title: '2026-09-01第1-2节', start_at: '{TUE_0800 - 14 * 86400}', sub_delete: '1'}},
  {{sub_id: '2', title: '2026-09-08第1-2节', start_at: '{TUE_0800 - 7 * 86400}', sub_show: 'no'}},
  {{sub_id: '4', title: '2026-09-15第1-2节', start_at: '{TUE_0800}', sub_delete: '0'}},
];
console.log(JSON.stringify([inferTermStartMonday(rows), inferTermStartMonday(null),
  teachingWeekOf('2026-09-20', '2026-09-14'), teachingWeekOf('2026-09-21', '2026-09-14'),
  teachingWeekOf('2026-09-13', '2026-09-14'), teachingWeekOf('2027-06-01', '2026-09-14'),
  teachingWeekOf('2026-11-05', '2026-09-16')]));
""")
        self.assertEqual(value, ["2026-09-14", None, 1, 2, None, None, 8])

    def test_filename_levels_and_sanitizing(self):
        value = self._run("""
const full = {date: '2026-09-17', weekday: '周四', periods: '1-2', startTime: '0800', teachingWeek: 1};
const noWeek = {...full, teachingWeek: null};
const noPeriods = {...noWeek, periods: null};
console.log(JSON.stringify([
  buildZipFilename('示例课程（H）2026', full, '20261104_0639'),
  buildZipFilename('示例课程（H）2026', noWeek, '20261104_0639'),
  buildZipFilename('示例课程（H）2026', noPeriods, '20261104_0639'),
  buildZipFilename('示例课程（H）2026', {date: null}, '20261104_0639'),
  buildZipFilename('a/b:c*  ', full, 'x').filename,
  buildZipFilename('', null, 'x').filename,
]));
""")
        self.assertEqual(value[0], {"filename": "示例课程（H）2026_第1周_2026-09-17_周四第1-2节_资产包.zip", "level": "week"})
        self.assertEqual(value[1], {"filename": "示例课程（H）2026_2026-09-17_周四第1-2节_资产包.zip", "level": "date"})
        self.assertEqual(value[2]["filename"], "示例课程（H）2026_2026-09-17_周四_0800_资产包.zip")
        self.assertEqual(value[3], {"filename": "示例课程（H）2026_20261104_0639_资产包.zip", "level": "export_time"})
        self.assertEqual(value[4], "a_b_c__第1周_2026-09-17_周四第1-2节_资产包.zip")
        self.assertEqual(value[5], "智云课程_x_资产包.zip")

    def _resolve(self, fetch_body: str, *, override: str = "null", record: str = "null", lecture_date: str = "null"):
        return self._run(f"""
const calls = [];
global.window = {{location: {{search: '?course_id=101&sub_id=202&tenant_code=1', origin: 'https://classroom.example.invalid'}},
                  localStorage: {{getItem: () => {override}}}}};
global.document = {{cookie: 'other=1; _token=' + encodeURIComponent('a:1:{{i:0;s:6:"_token";i:1;s:11:"aa.bbb.cccc";}}')}};
global.fetch = async (url, options) => {{
  calls.push({{url: url.replace(/\\?.*/, ''), auth: options.headers.Authorization || null, credentials: options.credentials}});
  {fetch_body}
}};
resolveLessonInfo({{lessonRecord: {record}, lectureDate: {lecture_date}}}).then(info => console.log(JSON.stringify({{info, calls}})));
""")

    def test_resolve_reads_sub_info_and_catalogue(self):
        out = self._resolve(f"""
  if (url.includes('get-sub-info')) return {{ok: true, json: async () => ({{data: {{sub_id: '202', sub_title: '2026-09-17第1-2节',
      start_at: '{THU_0800}', end_at: '{THU_0800 + 5700}'}}}})}};
  return {{ok: true, json: async () => ({{result: {{data: [{{sub_id: '201', title: '2026-09-15第1-2节', start_at: '{TUE_0800}'}}]}}}})}};
""", record="{subTitle: '2026-09-15第1-2节', startAt: null}")
        info = out["info"]
        self.assertEqual((info["date"], info["teachingWeek"], info["periods"]), ("2026-09-17", 1, "1-2"))
        self.assertEqual(info["termStartMonday"], "2026-09-14")
        self.assertTrue(all(call["auth"] == "Bearer aa.bbb.cccc" for call in out["calls"]))
        self.assertEqual(out["calls"][0]["credentials"], "include")
        self.assertNotIn("aa.bbb.cccc", json.dumps(info))

    def test_resolve_falls_back_when_api_unavailable(self):
        out = self._resolve("throw Error('offline');", record="{subTitle: '2026-09-22第3-4节', startAt: null}")
        info = out["info"]
        self.assertEqual((info["date"], info["periods"], info["teachingWeek"]), ("2026-09-22", "3-4", None))
        # Same-origin first, then the yjapi host, for both endpoints.
        self.assertEqual(len(out["calls"]), 4)
        self.assertIn("https://yjapi.cmc.zju.edu.cn/courseapi/v2/course/catalogue",
                      [call["url"] for call in out["calls"]])
        self.assertEqual(out["calls"][1]["credentials"], "omit")

    def test_resolve_ignores_sub_info_for_another_lesson_and_honours_override(self):
        out = self._resolve(f"""
  return {{ok: true, json: async () => ({{data: {{sub_id: '999', sub_title: '2026-01-01第9节', start_at: '{TUE_0800}'}}}})}};
""", override="'2026-09-16'", lecture_date="'2026-11-05'")
        info = out["info"]
        self.assertEqual((info["date"], info["dateSource"], info["periods"]), ("2026-11-05", "page", None))
        self.assertEqual((info["teachingWeek"], info["teachingWeekBasis"], info["termStartMonday"]),
                         (8, "user_override", "2026-09-14"))
        self.assertFalse(any("catalogue" in call["url"] for call in out["calls"]))


if __name__ == "__main__":
    unittest.main()
