"""Build a local review page: check disputed steps against the video and mark agree / disagree.

usage: python review_page.py RUN [RUN ...] --labels LABELS.csv [--video-root DIR] [--video VID_0083=PATH ...]
           [--items items.json] [--all] [--evidence] [--steps 1,2,10-14] [-o sop-video-grading/review/人工核對_YYYYMMDD.html]

RUN is a run-record folder (sop-video-grading/runs/VID_0083/<版本>/) or a grading.json; its sibling
segmentation.json gives the step times and the hands box. Each maintainer builds the page on their own
machine: it plays their local original video (--video; else found under --video-root by the label
sheet's relative path; else a sibling source.mp4; else the reviewer picks the file in the page) and
jumps to any time written in the explanations; nothing from the video is copied into the page. Items:
  - every step whose model verdict differs from the label sheet, or that the model flagged for review
    (--all: every step with a label);
  - --items adds Claude's proposals, a JSON list of
      {"type": "step", "video_id": "VID_0083", "step": 22, "proposal": "正確", "why": "..."}
      {"type": "standard", "id": "19b", "title": "...", "before": "...", "after": "...", "why": "..."}
The reviewer marks 同意建議 / 同意標記表 / 改判 (with the right verdict) / 待老師確認 (standards: 同意 /
不同意 / 待老師確認) and exports a CSV. Progress stays in the browser until exported.
--evidence reviews the model's evidence criterion by criterion instead: it also lists the steps the model
judged 錯誤 or 部分正確 (not removed or pending steps), and for every criterion the model or a rule judged
the reviewer picks the right verdict and whether the cited evidence holds (EVIDENCE); the CSV has one
row per criterion and review_accuracy.py turns the results into the system accuracy. --steps keeps only
those steps, so several reviewers can split the work by step.
The page holds video paths that may contain student names, so it is written to the gitignored
sop-video-grading/review/ by default.
"""
import argparse
import csv
import datetime
import json
import os
import sys
from pathlib import Path

from eval_grading import FAULTS, UNUSABLE_VIDEOS, load_labels
from rubric import NOT_APPLICABLE, PENDING, load_rubric, wrong_with_links

REVIEW_DIR = Path(__file__).resolve().parent.parent / "review"
VERDICTS = ["正確", "錯誤", "部分正確", "無法判定"]
EVIDENCE = ["正確", "時間不對", "內容不符", "無法確認"]  # 時間不對/內容不符 make the step wrong in review_accuracy.py
EVIDENCE_BAD = {"時間不對", "內容不符"}


def reviewable(criterion: dict) -> bool:
    """A criterion the evidence review asks about: judged by the model or a rule (not a fixed not_visible
    one) and not still pending. Same test as `asked` in the page."""
    return criterion.get("source") != "fixed" and criterion["verdict"] != PENDING


def parse_steps(text: str) -> set[int]:
    """'1,2,10-14' -> {1, 2, 10, 11, 12, 13, 14}."""
    out: set[int] = set()
    for part in filter(None, (p.strip() for p in text.split(","))):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def load_run(path: Path) -> tuple[dict, dict | None, Path]:
    """(grading, segmentation or None, folder) from a run folder or a grading.json."""
    folder = path if path.is_dir() else path.parent
    grading = json.loads((folder / "grading.json" if path.is_dir() else path).read_text(encoding="utf-8"))
    seg_path = folder / "segmentation.json"
    seg = json.loads(seg_path.read_text(encoding="utf-8")) if seg_path.exists() else None
    return grading, seg, folder


def step_spans(segmentation: dict | None, step_id: int) -> list[list[float]]:
    """Every [start, end] the segmentation found for the step (repeated steps have several entries)."""
    if not segmentation:
        return []
    return [[s["start"], s["end"]] for s in segmentation["steps"]
            if s["step_id"] == step_id and s.get("start") is not None and s.get("end") is not None]


def build_items(grading: dict, segmentation: dict | None, labels: dict[int, dict], rubric: dict,
                include_all: bool = False, run: str = "", *, evidence: bool = False,
                steps: set[int] | None = None) -> list[dict]:
    """`run` names the version (the record folder), so two versions of one video stay separate items.
    `evidence` also lists the model's 錯誤/部分正確 steps and drops removed and pending steps (nothing to
    review); `steps` keeps only those step ids."""
    names = {s["step_id"]: s["text"] for s in rubric["steps"]}
    texts = {c["id"]: c["text"] for s in rubric["steps"] for c in s["criteria"]}
    links = wrong_with_links(rubric)
    vid = grading["video_id"]
    items = []
    for step in grading["steps"]:
        sid, label = step["step_id"], labels.get(step["step_id"])
        if steps is not None and sid not in steps:
            continue
        if evidence and step["verdict"] in (NOT_APPLICABLE, PENDING):
            continue
        review = bool(step.get("needs_review"))
        differs = label is not None and label["verdict"] != step["verdict"]
        fault = evidence and step["verdict"] in FAULTS
        if not (differs or review or fault or (include_all and label)):
            continue
        items.append({
            "type": "step", "key": f"{vid}--{run}--{sid}", "run": run, "video_id": vid, "step": sid,
            "name": names.get(sid, ""),
            "label": label, "model": step["verdict"], "review": review,
            "criteria": [{"id": c["criterion_id"], "text": texts.get(c["criterion_id"], ""), "verdict": c["verdict"],
                          "explanation": c["explanation"], "review_reason": c.get("review_reason", ""),
                          "source": c.get("source", ""), "times": c.get("times") or [],
                          "wrong_with": links.get(c["criterion_id"], "")}
                         for c in step["criteria"]],
            "proposal": step["verdict"], "proposal_src": "模型", "why": "",
            "spans": step_spans(segmentation, sid)})
    return items


def merge_proposals(items: list[dict], extra: list[dict], rubric: dict) -> list[dict]:
    """Claude's step proposals replace the model's proposal on every version of that video and step
    (or add the step); standards are appended."""
    names = {s["step_id"]: s["text"] for s in rubric["steps"]}
    for e in extra:
        if e.get("type") == "standard":
            items.append({"type": "standard", "key": f"standard-{e['id']}", **{k: e.get(k, "") for k in
                          ("id", "title", "before", "after", "why")}})
            continue
        if e["proposal"] not in VERDICTS:
            raise ValueError(f"{e['video_id']} 步驟 {e['step']}：建議判定必須是 {'/'.join(VERDICTS)}")
        matches = [i for i in items if i["type"] == "step" and (i["video_id"], i["step"]) == (e["video_id"], e["step"])]
        if not matches:
            matches = [{"type": "step", "key": f"{e['video_id']}--Claude--{e['step']}", "run": "",
                        "video_id": e["video_id"], "step": e["step"], "name": names.get(e["step"], ""),
                        "label": None, "model": None, "review": False, "criteria": [], "spans": []}]
            items.append(matches[0])
        for item in matches:
            item.update(proposal=e["proposal"], proposal_src="Claude", why=e.get("why", ""))
    return items


def label_video_paths(labels_path: Path) -> dict[str, str]:
    """影片ID -> the label sheet's relative video path (column A)."""
    with open(labels_path, encoding="utf-8-sig", newline="") as f:
        return {r[14].strip(): r[0].strip().replace("\\", "/") for r in csv.reader(f)
                if len(r) > 14 and r[14].strip().startswith("VID_") and r[0].strip()}


def find_video(root: Path, relative: str) -> Path | None:
    """The original video under the reviewer's own folder; root may also be inside the sheet's top folder."""
    parts = Path(relative).parts
    for i in range(len(parts)):
        candidate = root.joinpath(*parts[i:])
        if candidate.is_file():
            return candidate
    return None


def video_src(video: Path, page_dir: Path) -> str:
    try:
        return Path(os.path.relpath(video.resolve(), page_dir.resolve())).as_posix()
    except ValueError:  # another drive on Windows
        return video.resolve().as_uri()


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__TITLE__", data["title"]).replace("__DATA__", payload)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--items", type=Path, help="Claude's proposals (JSON list, see above)")
    ap.add_argument("--video", action="append", default=[], help="VID_0083=PATH to the original video")
    ap.add_argument("--video-root", type=Path,
                    help="your folder of original videos; each is found by the label sheet's relative path")
    ap.add_argument("--all", action="store_true", help="every labelled step, not only the disputed ones")
    ap.add_argument("--evidence", action="store_true",
                    help="review each criterion's verdict and evidence; also lists the model's 錯誤/部分正確 steps")
    ap.add_argument("--steps", type=parse_steps, help="only these steps, e.g. 1,2,10-14")
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv[1:])
    if a.evidence and a.items:
        raise SystemExit("--evidence 核對模型的證據，不能同時給 --items")
    today = datetime.date.today().isoformat()
    out = a.out or REVIEW_DIR / f"人工核對_{today.replace('-', '')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    rubric = load_rubric()
    labels = load_labels(a.labels, set())
    given = dict(v.split("=", 1) for v in a.video)
    relative = label_video_paths(a.labels) if a.video_root else {}
    items, videos, skipped = [], {}, []
    for run in a.runs:
        grading, seg, folder = load_run(run)
        vid = grading["video_id"]
        if vid in UNUSABLE_VIDEOS:  # broken video file, not reviewed (AGENTS.md, issue #148)
            skipped.append(vid)
            continue
        items += build_items(grading, seg, labels.get(vid, {}), rubric, a.all, run=folder.name,
                             evidence=a.evidence, steps=a.steps)
        if vid in given:
            path = Path(given[vid])
        else:
            path = (find_video(a.video_root, relative[vid]) if vid in relative else None) or folder / "source.mp4"
        videos[vid] = {"src": video_src(path, out.parent) if path.exists() else "",
                       "hands": seg["meta"]["crop"]["hands_box"] if seg else None}
    if a.items:
        items = merge_proposals(items, json.loads(a.items.read_text(encoding="utf-8")), rubric)
    keys = [i["key"] for i in items]
    if len(keys) != len(set(keys)):
        raise SystemExit("同一支影片的同一個版本給了兩次（項目重複）：" + "、".join(sorted({k for k in keys if keys.count(k) > 1})))
    title = f"SOP 評分{'證據' if a.evidence else ''}人工核對 {today}"
    if a.steps:
        title += f"（步驟 {'、'.join(map(str, sorted(a.steps)))}）"
    data = {"title": title, "key": f"{out.name}", "rubric": rubric["version"], "items": items, "videos": videos,
            "mode": "evidence" if a.evidence else "verdict", "evidence": EVIDENCE}
    out.write_text(render(data), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    if skipped:
        print(f"略過不使用的影片（影片檔有問題，見 AGENTS.md）：{'、'.join(sorted(set(skipped)))}")
    missing = [v for v, info in videos.items() if not info["src"]]
    print(f"{out}：{len(items)} 項（{len(videos)} 支影片）" + (f"；找不到影片，要在頁面裡選檔：{'、'.join(missing)}"
                                                               if missing else ""))
    return 0


TEMPLATE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
:root{--bg:#f6f6f4;--fg:#1f2328;--mute:#5d646d;--card:#fff;--line:#e0e0dc;--accent:#2f6f9f;--accbg:#e6f0f8;
--ok:#1f7a4d;--okbg:#e3f3ea;--bad:#b3261e;--badbg:#fbe7e5;--warn:#8a5a00;--warnbg:#fdf1d8;--na:#5d646d;--nabg:#ececea}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#161718;--fg:#e8e8e6;--mute:#a1a7ae;--card:#202224;
--line:#36393c;--accent:#7fb3dc;--accbg:#1f3042;--ok:#7fd1a4;--okbg:#1d3a2b;--bad:#f3a59f;--badbg:#45211e;--warn:#f0c46b;
--warnbg:#3f3216;--na:#a1a7ae;--nabg:#2c2e30}}
:root[data-theme="dark"]{--bg:#161718;--fg:#e8e8e6;--mute:#a1a7ae;--card:#202224;--line:#36393c;--accent:#7fb3dc;--accbg:#1f3042;
--ok:#7fd1a4;--okbg:#1d3a2b;--bad:#f3a59f;--badbg:#45211e;--warn:#f0c46b;--warnbg:#3f3216;--na:#a1a7ae;--nabg:#2c2e30}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 "Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif}
.wrap{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:16px;padding:12px 16px;max-width:1700px;margin:auto}
@media (max-width:900px){.wrap{grid-template-columns:1fr}.left{position:static!important}}
.left{position:sticky;top:12px;align-self:start}
.vbox{position:relative;overflow:hidden;background:#000;border-radius:8px;aspect-ratio:16/9}
video{width:100%;height:100%;display:block;transform-origin:0 0}
.pick{position:absolute;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;gap:8px;color:#eee;text-align:center;padding:16px}
.pick.show{display:flex}
.ctl{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0}
input[type=range]{flex:1 1 100%}
button,select,input[type=text],textarea{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:4px 10px}
button{cursor:pointer}button:hover{border-color:var(--accent)}button.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.now{font-variant-numeric:tabular-nums;font-weight:700;font-size:18px;margin-right:6px}
.help{font-size:13px;color:var(--mute)}
.top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
h1{font-size:20px;margin:0 8px 0 0}.prog{font-size:14px;color:var(--mute)}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}
.item.active{border-color:var(--accent);box-shadow:0 0 0 2px var(--accbg)}.item.done{opacity:.75}
.ih{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.ih h2{font-size:16px;margin:0}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.pill{font-size:12px;font-weight:700;padding:1px 9px;border-radius:999px;white-space:nowrap}
.p正確{color:var(--ok);background:var(--okbg)}.p錯誤{color:var(--bad);background:var(--badbg)}.p部分正確{color:var(--warn);background:var(--warnbg)}
.p無法判定,.p不適用,.p待判定,.p無{color:var(--na);background:var(--nabg)}
.lbl{font-size:12px;color:var(--mute)}
.spans{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}.spans button{font-size:13px;padding:2px 8px}
.crit{border-top:1px dashed var(--line);padding-top:6px;margin-top:6px;font-size:14px}
.ct{color:var(--mute)}.t{color:var(--accent);cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.why{background:var(--accbg);border-radius:6px;padding:6px 10px;margin:6px 0;font-size:14px}
.pre{white-space:pre-wrap;font-size:14px;border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin:4px 0}
.dec{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
textarea{width:100%;min-height:2.4em;margin-top:6px;resize:vertical}
</style></head><body>
<div class="wrap">
<div class="left">
<div class="vbox"><video id="v" preload="auto" playsinline></video>
<div class="pick" id="pick"><div id="pickmsg"></div><input type="file" id="file" accept="video/*"></div></div>
<div class="ctl"><span class="now" id="now">00:00.0</span>
<button id="play">播放</button>
<button data-d="-5">−5s</button><button data-d="-1">−1s</button><button data-d="-0.2">−0.2s</button>
<button data-d="0.2">+0.2s</button><button data-d="1">+1s</button><button data-d="5">+5s</button>
<select id="rate"><option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select>
<button id="zoom">手部放大</button>
<input type="range" id="seek" min="0" max="1" step="0.1" value="0"></div>
<div class="help">點藍色時間會跳到那裡（往前 1 秒）。鍵盤：空白鍵 播放／暫停，← → 前後 1 秒，, . 前後 0.2 秒。短動作（彈針、插針）可調 0.25× 慢放，或按「手部放大」。</div>
</div>
<div class="right">
<div class="top"><h1 id="title"></h1><span class="prog" id="prog"></span>
<input type="text" id="who" placeholder="核對者" size="8">
<select id="filter"><option value="todo">還沒核對的</option><option value="all">全部</option><option value="done">已核對</option></select>
<button id="export">匯出 CSV</button></div>
<div id="items"></div>
</div></div>
<script>
const D = __DATA__;
const VERDICTS = ["正確", "錯誤", "部分正確", "無法判定"];
const KEY = "sop-review:" + D.key;
let S = {decisions: {}, who: ""};
try { S = Object.assign(S, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} };
const byKey = Object.fromEntries(D.items.map(it => [it.key, it]));
// A decision belongs to what the reviewer saw: when a rebuilt page shows another proposal, model or label
// verdict (or standard text), the old decision no longer applies and the item is to be reviewed again.
const EV = D.mode === "evidence";
const dk = it => [it.key, it.proposal, it.model, (it.label || {}).verdict, it.before, it.after].join("|") +
  (EV ? "|" + (it.criteria || []).map(c => c.id + "=" + c.verdict).join(",") : "");
const got = it => S.decisions[dk(it)] || {};
// evidence mode: a criterion is asked about unless a fixed not_visible one or still pending (reviewable in review_page.py)
const asked = c => c.source !== "fixed" && c.verdict !== "待判定";
const critDone = (d, c) => { const x = (d.crit || {})[c.id] || {}; return !!(x.verdict && x.evidence); };
const isDone = it => {
  const d = got(it);
  if (!EV || it.type !== "step") return !!d.decision;
  return d.decision === "待老師確認" || it.criteria.filter(asked).every(c => critDone(d, c));
};
const dec = key => { const k = dk(byKey[key]); return S.decisions[k] = S.decisions[k] || {}; };
const $ = id => document.getElementById(id);
const v = $("v"), picked = {};
let current = null, zoomed = false;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = t => { const m = Math.floor(t / 60), s = t - m * 60; return String(m).padStart(2, "0") + ":" + s.toFixed(1).padStart(4, "0"); };
const sec = (m, s) => Number(m) * 60 + Number(s);
// mm:ss or mm:ss–mm:ss in the text become links, except clip-relative times: same rules as cited_spans in eval_grading.py
function clipRanges(str) {
  const t = String.raw`\d{2}:\d{2}(?:\.\d+)?(?:\s*[–\-~～至到]\s*\d{2}:\d{2}(?:\.\d+)?)?`, head = String.raw`片段(?:內[^\d]{0,3}|\s*)` + t;
  const list = new RegExp(head + String.raw`(?:\s*[、及和與]\s*` + t + ")*", "g"), ranges = [];
  for (const p of str.matchAll(/[（(][^（()）]*[）)]/g))
    for (const m of p[0].matchAll(list)) ranges.push([p.index + m.index, p.index + m.index + m[0].length]);
  for (const m of str.matchAll(new RegExp(head, "g"))) ranges.push([m.index, m.index + m[0].length]);
  return ranges;
}
// Times after clip@A-B (「clip@105.5-113.5 的 00:02.5–00:07.9 及 00:09」) are relative to the clip and link to A + t
// when 片段 follows the clip, or when they fit in the clip's length and are not inside A-B as video times
// (「clip@68-100 中：01:08.0」 is a video time): clip_relative in eval_grading.py
function clipAtRanges(str) {
  const t = String.raw`\d{2}:\d{2}(?:\.\d+)?(?:\s*[–\-~～至到]\s*\d{2}:\d{2}(?:\.\d+)?)?`;
  const re = new RegExp(String.raw`clip@(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)[^\d@（()），,。；;]{0,8}?` + t + String.raw`(?:\s*[、及和與]\s*` + t + ")*", "g");
  return [...str.matchAll(re)].map(m => [m.index, m.index + m[0].length, Number(m[1]), Number(m[2]), m[0].includes("片段")]);
}
function linkTimes(text, vid) {
  const safe = esc(text), clips = clipRanges(safe), ats = clipAtRanges(safe);
  return safe.replace(/(\d{1,2}):(\d{2}(?:\.\d+)?)(?:\s*[–-]\s*(\d{1,2}):(\d{2}(?:\.\d+)?))?/g, (all, m1, s1, m2, s2, off) => {
    const at = ats.find(([a, b]) => off >= a && off < b);
    const start = sec(m1, s1), end = m2 ? sec(m2, s2) : start;
    if (at && (at[4] || (Math.max(start, end) <= at[3] - at[2] + 0.5 && !(at[2] - 1 <= start && start <= at[3] + 1)))) {
      const t = at[2] + start;
      return `<span class="t" data-vid="${vid}" data-t="${t}" title="影片 ${fmt(t)}">${all}</span>`;
    }
    if (at) return `<span class="t" data-vid="${vid}" data-t="${start}">${all}</span>`;
    if (clips.some(([a, b]) => off >= a && off < b)) return all;
    return `<span class="t" data-vid="${vid}" data-t="${sec(m1, s1)}">${all}</span>`;
  });
}
const pill = (verdict, prefix) => `<span class="lbl">${prefix}</span><span class="pill p${esc(verdict || "無")}">${esc(verdict || "無")}</span>`;

function load(vid) {
  if (current === vid) return true;
  const info = D.videos[vid] || {};
  const src = picked[vid] || info.src;
  current = vid;
  applyZoom();
  if (!src) {
    v.removeAttribute("src"); v.load();
    $("pickmsg").textContent = `找不到 ${vid} 的影片，請選擇原始影片檔`;
    $("pick").classList.add("show");
    return false;
  }
  $("pick").classList.remove("show");
  v.src = src;
  return true;
}
function jump(vid, t) {
  if (!load(vid)) return;
  const go = () => { v.currentTime = Math.max(0, t - 1); };
  if (v.readyState >= 1) go(); else v.addEventListener("loadedmetadata", go, {once: true});
}
function applyZoom() {
  const box = current && D.videos[current] && D.videos[current].hands;
  $("zoom").classList.toggle("on", zoomed);
  if (!zoomed || !box) { v.style.transform = ""; return; }
  const s = Math.min(1 / (box[2] - box[0]), 1 / (box[3] - box[1]));
  v.style.transform = `scale(${s}) translate(${-box[0] * 100}%, ${-box[1] * 100}%)`;
}
$("file").addEventListener("change", e => {
  const f = e.target.files[0]; if (!f || !current) return;
  picked[current] = URL.createObjectURL(f);
  const vid = current; current = null; load(vid);
});

function decisionButtons(it) {
  const d = got(it);
  const b = (val, text) => `<button data-k="${esc(it.key)}" data-dec="${esc(val)}" class="${d.decision === val ? "on" : ""}">${esc(text)}</button>`;
  if (EV) return b("待老師確認", "整步待老師確認");
  if (it.type === "standard") return b("同意", "同意") + b("不同意", "不同意") + b("待老師確認", "待老師確認");
  let h = b("同意建議", `同意建議（${it.proposal}）`);
  if (it.label && it.label.verdict !== it.proposal) h += b("同意標記表", `同意標記表（${it.label.verdict}）`);
  h += b("改判", "改判") + b("待老師確認", "待老師確認");
  if (d.decision === "改判") h += `<select data-k="${esc(it.key)}" class="fix"><option value="">正確判定…</option>` +
    VERDICTS.map(x => `<option ${d.verdict === x ? "selected" : ""}>${x}</option>`).join("") + "</select>";
  return h;
}
// evidence mode: the reviewer's verdict and whether the cited evidence holds, for one criterion
function critControls(it, c) {
  const x = (got(it).crit || {})[c.id] || {};
  const b = (field, val, text) => `<button data-k="${esc(it.key)}" data-c="${esc(c.id)}" data-f="${field}" data-val="${esc(val)}"` +
    ` class="${x[field] === val ? "on" : ""}">${esc(text)}</button>`;
  let h = "";
  if (c.times.length) h += `<div class="spans"><span class="lbl">模型引用時間</span>` +
    c.times.map(([a, e]) => `<button class="t" data-vid="${esc(it.video_id)}" data-t="${a}">${fmt(a)}–${fmt(e)}</button>`).join("") + "</div>";
  if (c.wrong_with) h += `<div class="help">${esc(c.wrong_with)} 錯誤時這一條也算錯誤（連動）；這裡只判這一條本身。</div>`;
  return h + `<div class="dec"><span class="lbl">人工判定</span>` +
    VERDICTS.map(v => b("verdict", v, v === c.verdict ? v + "（同模型）" : v)).join("") + "</div>" +
    `<div class="dec"><span class="lbl">證據</span>` + D.evidence.map(v => b("evidence", v, v)).join("") + "</div>";
}
function card(it) {
  const d = got(it);
  let h = `<div class="item${isDone(it) ? " done" : ""}" id="i-${esc(it.key)}" data-vid="${esc(it.video_id || "")}">`;
  if (it.type === "standard") {
    h += `<div class="ih"><h2>標準 ${esc(it.id)}：${esc(it.title)}</h2></div>` +
      `<div class="lbl">修改前</div><div class="pre">${esc(it.before)}</div>` +
      `<div class="lbl">修改後</div><div class="pre">${esc(it.after)}</div>` +
      (it.why ? `<div class="why">${esc(it.why)}</div>` : "");
  } else {
    h += `<div class="ih"><h2>${esc(it.video_id)} 步驟 ${it.step}</h2><span class="ct">${esc(it.name)}</span>` +
      `${it.run ? `<span class="lbl">${esc(it.run)}</span>` : ""}</div>` +
      `<div class="pills">${pill(it.label && it.label.verdict, "標記表")}${pill(it.model, "模型")}` +
      `${it.review ? '<span class="pill p部分正確">模型要求人工複核</span>' : ""}${pill(it.proposal, "建議（" + it.proposal_src + "）")}</div>`;
    if (it.spans.length) h += `<div class="spans"><span class="lbl">切分時間</span>` +
      it.spans.map(([a, b]) => `<button class="t" data-vid="${esc(it.video_id)}" data-t="${a}">${fmt(a)}–${fmt(b)}</button>`).join("") + "</div>";
    if (it.why) h += `<div class="why"><b>Claude：</b>${linkTimes(it.why, it.video_id)}</div>`;
    if (it.label && it.label.why) h += `<div class="crit"><b>標記表說明：</b>${esc(it.label.why)}</div>`;
    for (const c of it.criteria) h += `<div class="crit">${pill(c.verdict, c.id)} <span class="ct">${esc(c.text)}</span><br>` +
      linkTimes(c.explanation, it.video_id) + (c.review_reason ? `<br><b>需複核：</b>${linkTimes(c.review_reason, it.video_id)}` : "") +
      (EV && asked(c) ? critControls(it, c) : "") + "</div>";
  }
  h += `<div class="dec">${decisionButtons(it)}</div>` +
    `<textarea data-k="${esc(it.key)}" placeholder="備註（不同意的理由、正確的時間點…）">${esc(d.note || "")}</textarea></div>`;
  return h;
}
function progress() {
  const done = D.items.filter(isDone).length;
  $("prog").textContent = `已核對 ${done}/${D.items.length}　rubric ${D.rubric}`;
}
function render() {
  const f = $("filter").value;
  const shown = D.items.filter(it => f === "all" || (f === "done") === isDone(it));
  $("items").innerHTML = shown.map(card).join("") || '<p class="help">沒有項目。</p>';
  progress();
}
// A decision redraws only its own card, so it stays on screen for the verdict and the note;
// the filter hides it at the next render (changing the filter).
function refresh(key) {
  const el = document.getElementById("i-" + key);
  const active = el.classList.contains("active");
  el.outerHTML = card(byKey[key]);
  if (active) document.getElementById("i-" + key).classList.add("active");
  progress();
}
$("title").textContent = D.title;
$("who").value = S.who || "";
$("who").addEventListener("input", e => { S.who = e.target.value; save(); });
$("filter").addEventListener("change", render);
$("items").addEventListener("click", e => {
  const t = e.target.closest(".t");
  if (t) { jump(t.dataset.vid, Number(t.dataset.t)); return; }
  const cb = e.target.closest("button[data-c]");
  if (cb) {
    const d = dec(cb.dataset.k), crit = d.crit = d.crit || {}, x = crit[cb.dataset.c] = crit[cb.dataset.c] || {};
    x[cb.dataset.f] = x[cb.dataset.f] === cb.dataset.val ? "" : cb.dataset.val;
    d.at = new Date().toISOString(); save(); refresh(cb.dataset.k); return;
  }
  const btn = e.target.closest("button[data-dec]");
  if (btn) {
    const d = dec(btn.dataset.k);
    d.decision = d.decision === btn.dataset.dec ? "" : btn.dataset.dec;
    if (d.decision !== "改判") delete d.verdict;
    d.at = new Date().toISOString(); save(); refresh(btn.dataset.k); return;
  }
  const box = e.target.closest(".item");
  if (box && box.dataset.vid) {
    document.querySelectorAll(".item.active").forEach(x => x.classList.remove("active"));
    box.classList.add("active"); load(box.dataset.vid);
  }
});
$("items").addEventListener("change", e => {
  if (!e.target.classList.contains("fix")) return;
  dec(e.target.dataset.k).verdict = e.target.value; save();
});
$("items").addEventListener("input", e => {
  if (e.target.tagName !== "TEXTAREA") return;
  const d = dec(e.target.dataset.k);
  d.note = e.target.value; save();
});
$("play").addEventListener("click", () => v.paused ? v.play() : v.pause());
v.addEventListener("play", () => $("play").textContent = "暫停");
v.addEventListener("pause", () => $("play").textContent = "播放");
v.addEventListener("loadedmetadata", () => { $("seek").max = v.duration || 1; });
const showTime = () => { $("now").textContent = fmt(v.currentTime); $("seek").value = v.currentTime; };
v.addEventListener("timeupdate", showTime);
v.addEventListener("seeked", showTime);
$("seek").addEventListener("input", e => { v.currentTime = Number(e.target.value); });
document.querySelectorAll("[data-d]").forEach(b => b.addEventListener("click", () => { v.currentTime = Math.max(0, v.currentTime + Number(b.dataset.d)); }));
$("rate").addEventListener("change", e => { v.playbackRate = Number(e.target.value); });
$("zoom").addEventListener("click", () => { zoomed = !zoomed; applyZoom(); });
document.addEventListener("keydown", e => {
  if (["TEXTAREA", "SELECT", "INPUT"].includes(e.target.tagName)) return;
  const d = {ArrowLeft: -1, ArrowRight: 1, ",": -0.2, ".": 0.2}[e.key];
  if (d !== undefined) { v.currentTime = Math.max(0, v.currentTime + d); e.preventDefault(); }
  else if (e.key === " ") { v.paused ? v.play() : v.pause(); e.preventDefault(); }
});
$("export").addEventListener("click", () => {
  const q = x => `"${String(x ?? "").replace(/"/g, '""')}"`;
  if (EV) return download(q, evidenceRows());
  const rows = [["類型", "影片ID", "版本", "步驟", "標準", "標記表", "模型", "建議", "建議來源", "決定", "正確判定", "備註", "核對者", "核對時間"]];
  for (const it of D.items) {
    const d = got(it);
    const verdict = d.decision === "同意建議" ? it.proposal : d.decision === "同意標記表" ? (it.label || {}).verdict : d.verdict;
    rows.push(it.type === "standard"
      ? ["標準", "", "", "", it.id, "", "", it.after, "Claude", d.decision, "", d.note, S.who, d.at]
      : ["步驟", it.video_id, it.run, it.step, "", (it.label || {}).verdict, it.model, it.proposal, it.proposal_src,
         d.decision, verdict, d.note, S.who, d.at]);
  }
  download(q, rows);
});
// one row per asked criterion; the step's 待老師確認 and note repeat on each row (read by review_accuracy.py)
function evidenceRows() {
  const rows = [["類型", "影片ID", "版本", "步驟", "標準", "標記表", "模型步驟", "模型", "人工判定", "證據", "決定", "備註", "核對者", "核對時間"]];
  for (const it of D.items) {
    const d = got(it);
    for (const c of it.criteria.filter(asked)) {
      const x = (d.crit || {})[c.id] || {};
      rows.push(["證據", it.video_id, it.run, it.step, c.id, (it.label || {}).verdict, it.model, c.verdict,
                 x.verdict, x.evidence, d.decision, d.note, S.who, d.at]);
    }
  }
  return rows;
}
function download(q, rows) {
  const blob = new Blob(["﻿" + rows.map(r => r.map(q).join(",")).join("\r\n")], {type: "text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = D.key.replace(/\.html$/, "") + "_結果.csv"; a.click();
}
render();
const first = D.items.find(it => it.video_id);
if (first) load(first.video_id);
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main(sys.argv))
