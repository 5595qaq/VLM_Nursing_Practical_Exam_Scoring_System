"""Build a local review page: check disputed steps against the video and mark agree / disagree.

usage: python review_page.py RUN [RUN ...] --labels LABELS.csv [--video-root DIR] [--video VID_0083=PATH ...]
           [--items items.json] [--all] [-o sop-video-grading/review/人工核對_YYYYMMDD.html]

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

from eval_grading import load_labels
from rubric import load_rubric

REVIEW_DIR = Path(__file__).resolve().parent.parent / "review"
VERDICTS = ["正確", "錯誤", "部分正確", "無法判定"]


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
                include_all: bool = False) -> list[dict]:
    names = {s["step_id"]: s["text"] for s in rubric["steps"]}
    texts = {c["id"]: c["text"] for s in rubric["steps"] for c in s["criteria"]}
    vid = grading["video_id"]
    items = []
    for step in grading["steps"]:
        sid, label = step["step_id"], labels.get(step["step_id"])
        review = bool(step.get("needs_review"))
        differs = label is not None and label["verdict"] != step["verdict"]
        if not (differs or review or (include_all and label)):
            continue
        items.append({
            "type": "step", "key": f"{vid}-{sid}", "video_id": vid, "step": sid, "name": names.get(sid, ""),
            "label": label, "model": step["verdict"], "review": review,
            "criteria": [{"id": c["criterion_id"], "text": texts.get(c["criterion_id"], ""), "verdict": c["verdict"],
                          "explanation": c["explanation"], "review_reason": c.get("review_reason", "")}
                         for c in step["criteria"]],
            "proposal": step["verdict"], "proposal_src": "模型", "why": "",
            "spans": step_spans(segmentation, sid)})
    return items


def merge_proposals(items: list[dict], extra: list[dict], rubric: dict) -> list[dict]:
    """Claude's step proposals replace the model's proposal (or add the step); standards are appended."""
    names = {s["step_id"]: s["text"] for s in rubric["steps"]}
    by_key = {i["key"]: i for i in items}
    for e in extra:
        if e.get("type") == "standard":
            items.append({"type": "standard", "key": f"standard-{e['id']}", **{k: e.get(k, "") for k in
                          ("id", "title", "before", "after", "why")}})
            continue
        if e["proposal"] not in VERDICTS:
            raise ValueError(f"{e['video_id']} 步驟 {e['step']}：建議判定必須是 {'/'.join(VERDICTS)}")
        key = f"{e['video_id']}-{e['step']}"
        item = by_key.get(key)
        if item is None:
            item = {"type": "step", "key": key, "video_id": e["video_id"], "step": e["step"],
                    "name": names.get(e["step"], ""), "label": None, "model": None, "review": False,
                    "criteria": [], "spans": []}
            items.append(item)
            by_key[key] = item
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
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv[1:])
    today = datetime.date.today().isoformat()
    out = a.out or REVIEW_DIR / f"人工核對_{today.replace('-', '')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    rubric = load_rubric()
    labels = load_labels(a.labels, set())
    given = dict(v.split("=", 1) for v in a.video)
    relative = label_video_paths(a.labels) if a.video_root else {}
    items, videos = [], {}
    for run in a.runs:
        grading, seg, folder = load_run(run)
        vid = grading["video_id"]
        items += build_items(grading, seg, labels.get(vid, {}), rubric, a.all)
        if vid in given:
            path = Path(given[vid])
        else:
            path = (find_video(a.video_root, relative[vid]) if vid in relative else None) or folder / "source.mp4"
        videos[vid] = {"src": video_src(path, out.parent) if path.exists() else "",
                       "hands": seg["meta"]["crop"]["hands_box"] if seg else None}
    if a.items:
        items = merge_proposals(items, json.loads(a.items.read_text(encoding="utf-8")), rubric)
    data = {"title": f"SOP 評分人工核對 {today}", "key": f"{out.name}", "rubric": rubric["version"],
            "items": items, "videos": videos}
    out.write_text(render(data), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
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
const $ = id => document.getElementById(id);
const v = $("v"), picked = {};
let current = null, zoomed = false;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = t => { const m = Math.floor(t / 60), s = t - m * 60; return String(m).padStart(2, "0") + ":" + s.toFixed(1).padStart(4, "0"); };
const sec = (m, s) => Number(m) * 60 + Number(s);
// mm:ss or mm:ss–mm:ss in the text become links; times inside a clip ("片段內 00:03") are clip-relative, so not linked
function linkTimes(text, vid) {
  return esc(text).replace(/(\d{1,2}):(\d{2}(?:\.\d+)?)(?:\s*[–-]\s*(\d{1,2}):(\d{2}(?:\.\d+)?))?/g, (all, m1, s1, m2, s2, off, str) => {
    if (/片段內?\s*$/.test(str.slice(Math.max(0, off - 6), off))) return all;
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
  const d = S.decisions[it.key] || {};
  const b = (val, text) => `<button data-k="${esc(it.key)}" data-dec="${esc(val)}" class="${d.decision === val ? "on" : ""}">${esc(text)}</button>`;
  if (it.type === "standard") return b("同意", "同意") + b("不同意", "不同意") + b("待老師確認", "待老師確認");
  let h = b("同意建議", `同意建議（${it.proposal}）`);
  if (it.label && it.label.verdict !== it.proposal) h += b("同意標記表", `同意標記表（${it.label.verdict}）`);
  h += b("改判", "改判") + b("待老師確認", "待老師確認");
  if (d.decision === "改判") h += `<select data-k="${esc(it.key)}" class="fix"><option value="">正確判定…</option>` +
    VERDICTS.map(x => `<option ${d.verdict === x ? "selected" : ""}>${x}</option>`).join("") + "</select>";
  return h;
}
function card(it) {
  const d = S.decisions[it.key] || {};
  let h = `<div class="item${d.decision ? " done" : ""}" id="i-${esc(it.key)}" data-vid="${esc(it.video_id || "")}">`;
  if (it.type === "standard") {
    h += `<div class="ih"><h2>標準 ${esc(it.id)}：${esc(it.title)}</h2></div>` +
      `<div class="lbl">修改前</div><div class="pre">${esc(it.before)}</div>` +
      `<div class="lbl">修改後</div><div class="pre">${esc(it.after)}</div>` +
      (it.why ? `<div class="why">${esc(it.why)}</div>` : "");
  } else {
    h += `<div class="ih"><h2>${esc(it.video_id)} 步驟 ${it.step}</h2><span class="ct">${esc(it.name)}</span></div>` +
      `<div class="pills">${pill(it.label && it.label.verdict, "標記表")}${pill(it.model, "模型")}` +
      `${it.review ? '<span class="pill p部分正確">模型要求人工複核</span>' : ""}${pill(it.proposal, "建議（" + it.proposal_src + "）")}</div>`;
    if (it.spans.length) h += `<div class="spans"><span class="lbl">切分時間</span>` +
      it.spans.map(([a, b]) => `<button class="t" data-vid="${esc(it.video_id)}" data-t="${a}">${fmt(a)}–${fmt(b)}</button>`).join("") + "</div>";
    if (it.why) h += `<div class="why"><b>Claude：</b>${linkTimes(it.why, it.video_id)}</div>`;
    if (it.label && it.label.why) h += `<div class="crit"><b>標記表說明：</b>${esc(it.label.why)}</div>`;
    for (const c of it.criteria) h += `<div class="crit">${pill(c.verdict, c.id)} <span class="ct">${esc(c.text)}</span><br>` +
      linkTimes(c.explanation, it.video_id) + (c.review_reason ? `<br><b>需複核：</b>${linkTimes(c.review_reason, it.video_id)}` : "") + "</div>";
  }
  h += `<div class="dec">${decisionButtons(it)}</div>` +
    `<textarea data-k="${esc(it.key)}" placeholder="備註（不同意的理由、正確的時間點…）">${esc(d.note || "")}</textarea></div>`;
  return h;
}
function progress() {
  const done = D.items.filter(it => (S.decisions[it.key] || {}).decision).length;
  $("prog").textContent = `已核對 ${done}/${D.items.length}　rubric ${D.rubric}`;
}
function render() {
  const f = $("filter").value;
  const shown = D.items.filter(it => f === "all" || (f === "done") === !!(S.decisions[it.key] || {}).decision);
  $("items").innerHTML = shown.map(card).join("") || '<p class="help">沒有項目。</p>';
  progress();
}
// A decision redraws only its own card, so it stays on screen for the verdict and the note;
// the filter hides it at the next render (changing the filter).
function refresh(key) {
  const el = document.getElementById("i-" + key);
  const active = el.classList.contains("active");
  el.outerHTML = card(D.items.find(it => it.key === key));
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
  const btn = e.target.closest("button[data-dec]");
  if (btn) {
    const d = S.decisions[btn.dataset.k] = S.decisions[btn.dataset.k] || {};
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
  S.decisions[e.target.dataset.k].verdict = e.target.value; save();
});
$("items").addEventListener("input", e => {
  if (e.target.tagName !== "TEXTAREA") return;
  const d = S.decisions[e.target.dataset.k] = S.decisions[e.target.dataset.k] || {};
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
  const rows = [["類型", "影片ID", "步驟", "標準", "標記表", "模型", "建議", "建議來源", "決定", "正確判定", "備註", "核對者", "核對時間"]];
  for (const it of D.items) {
    const d = S.decisions[it.key] || {};
    const verdict = d.decision === "同意建議" ? it.proposal : d.decision === "同意標記表" ? (it.label || {}).verdict : d.verdict;
    rows.push(it.type === "standard"
      ? ["標準", "", "", it.id, "", "", it.after, "Claude", d.decision, "", d.note, S.who, d.at]
      : ["步驟", it.video_id, it.step, "", (it.label || {}).verdict, it.model, it.proposal, it.proposal_src,
         d.decision, verdict, d.note, S.who, d.at]);
  }
  const blob = new Blob(["﻿" + rows.map(r => r.map(q).join(",")).join("\r\n")], {type: "text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = D.key.replace(/\.html$/, "") + "_結果.csv"; a.click();
});
render();
const first = D.items.find(it => it.video_id);
if (first) load(first.video_id);
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main(sys.argv))
