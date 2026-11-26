"""Issue #134 human review page: blinded evidence review (說明層) + disagreement attribution.

usage: python review_blind.py --labels LABELS.csv=VID_a,VID_b [--labels ...] --video-root DIR
           [--n 60] [--seed 20261126] -o sop-video-grading/review/核對_wholevideo134_YYYYMMDD.html

Part 1 (blinded): for the decomposed arm and whole-video run1, sample --n criteria each from the steps whose
verdict agrees with the human verdict (only criteria the model decided itself: source vlm, not 待判定/不適用),
half from the short-action / hand-posture steps. Each video gets a random A/B letter per arm; tool traces
(clip@, zoom@, 片段內 ...) are stripped from the explanation so the wording does not give the arm away. The
letter-to-arm key is written next to the page as <page>_key.json, not into the page.
Part 2 (not blinded): every step where any arm disagrees with the human verdict, or agrees but its time does
not match (eval_grading strict), with all three arms side by side; the reviewer gives the verdict after
checking the video and ticks the causes.

The page plays the reviewer's local original video (found under --video-root by the label sheet's relative
path) and holds that path, which may contain a student name, so write it to the gitignored
sop-video-grading/review/ and do not share or commit it. Progress stays in the browser until exported as CSV.
"""
import argparse
import datetime
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
from eval_grading import compare, find_segmentation, load_labels  # noqa: E402
from review_page import find_video, label_video_paths, video_src  # noqa: E402
from rubric import load_rubric  # noqa: E402

RUNS = HERE.parent / "runs"
WHOLE = "rubric1.23_skillwhole0.3-s0.39_gemini-4-argon-static-fps2-high-up10fps-crf28_seg-whole-e2e"
DECOMP = {"0.38": "rubric1.23_skill0.38_gemini-4-argon_seg-skill1.5.6_gemini-4-argon",
          "0.39": "rubric1.23_skill0.39_gemini-4-argon_seg-skill1.5.6_gemini-4-argon"}
USE_039 = {"VID_0124", "VID_0125", "VID_0139"}  # work 1 used 0.39 only for these (see the experiment record)
SHORT = {12, 15, 16, 17, 18, 19, 22, 25, 26, 27, 28}
ARMS = ("拆解", "整支run1", "整支run2")
BLIND_ARMS = ("拆解", "整支run1")

_TOOL_PAREN = re.compile(r"[（(][^（()）]*(?:@|片段內|片段)[^（()）]*[）)]")
_TOOL_WORD = re.compile(r"(?:經\s*)?(?<![A-Za-z])(?:zoom|clip|burst|overview)(?![A-Za-z@])\s*", re.I)
_TOOL_TOKEN = re.compile(r"\b(?:clip|zoom|burst|ov|overview|frame|peak|audio)@[\w.\-#~]+(?:\s*#\d+)?")


def strip_tools(text: str) -> str:
    """Drop tool traces that name the decomposed arm's tools; keep the video times and the description."""
    text = _TOOL_PAREN.sub("", text)
    text = _TOOL_TOKEN.sub("", text)
    text = _TOOL_WORD.sub("", text)
    text = re.sub(r"[、，,]\s*(?=[、，,。；）)])", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def arm_paths(vid: str) -> dict[str, Path]:
    d = DECOMP["0.39" if vid in USE_039 else "0.38"]
    return {"拆解": RUNS / vid / d / "grading.json", "整支run1": RUNS / vid / WHOLE / "run1" / "grading.json",
            "整支run2": RUNS / vid / WHOLE / "run2" / "grading.json"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", action="append", required=True, help="LABELS.csv=VID_a,VID_b")
    ap.add_argument("--video-root", type=Path, required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20261126)
    ap.add_argument("-o", "--out", type=Path, required=True)
    a = ap.parse_args(argv)
    rubric = load_rubric()
    names = {s["step_id"]: s["text"] for s in rubric["steps"]}
    texts = {c["id"]: c["text"] for s in rubric["steps"] for c in s["criteria"]}
    rng = random.Random(a.seed)

    labels, rel, vids = {}, {}, []
    for spec in a.labels:
        path, vs = spec.rsplit("=", 1)
        loaded = load_labels(Path(path), set())
        paths = label_video_paths(Path(path))
        for v in vs.split(","):
            labels[v], rel[v] = loaded.get(v, {}), paths.get(v)
            vids.append(v)

    out = a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    videos, gradings, rows = {}, {}, {}
    for vid in vids:
        video = find_video(a.video_root, rel[vid]) if rel[vid] else None
        videos[vid] = {"src": video_src(video, out.parent) if video else ""}
        for arm, p in arm_paths(vid).items():
            g = json.loads(p.read_text(encoding="utf-8"))
            seg, _ = find_segmentation(p, vid)
            gradings[(vid, arm)] = g
            rows[(vid, arm)] = {r["step_id"]: r for r in compare(g, labels[vid], seg)}

    def human(vid, sid):
        h = labels[vid].get(sid)
        return {"verdict": h["verdict"], "why": h["why"], "spans": h["spans"]} if h else None

    # Part 1: blinded evidence sample
    letters = {vid: dict(zip(BLIND_ARMS, rng.sample(["A", "B"], 2))) for vid in vids}
    evidence, key = [], {"seed": a.seed, "n": a.n, "letters": letters, "items": {}}
    for arm in BLIND_ARMS:
        pool = {True: [], False: []}
        for vid in vids:
            for step in gradings[(vid, arm)]["steps"]:
                r = rows[(vid, arm)].get(step["step_id"])
                if not r or not r["match"]:
                    continue
                for c in step["criteria"]:
                    if c.get("source") == "vlm" and c["verdict"] not in ("待判定", "不適用"):
                        pool[step["step_id"] in SHORT].append((vid, step["step_id"], c))
        half = a.n // 2
        picked = rng.sample(pool[True], min(half, len(pool[True]))) + \
            rng.sample(pool[False], min(a.n - half, len(pool[False])))
        key[f"pool_{arm}"] = {"short": len(pool[True]), "other": len(pool[False])}
        for vid, sid, c in picked:
            k = f"E-{vid}-{c['criterion_id']}-{letters[vid][arm]}"
            key["items"][k] = arm
            evidence.append({"key": k, "video_id": vid, "step": sid, "name": names.get(sid, ""),
                             "criterion": c["criterion_id"], "text": texts.get(c["criterion_id"], ""),
                             "group": letters[vid][arm], "verdict": c["verdict"],
                             "explanation": strip_tools(c["explanation"]),
                             "times": c.get("times") or [], "human": human(vid, sid), "short": sid in SHORT})
    evidence.sort(key=lambda e: (e["video_id"], e["step"], e["criterion"], e["group"]))

    # Part 2: disagreements (any arm strict-mismatched)
    disputes = []
    for vid in vids:
        for sid in sorted(labels[vid]):
            arms = {arm: rows[(vid, arm)].get(sid) for arm in ARMS}
            if all(r and r["strict_match"] for r in arms.values()):
                continue
            entry = {"key": f"D-{vid}-{sid}", "video_id": vid, "step": sid, "name": names.get(sid, ""),
                     "human": human(vid, sid), "arms": {}}
            for arm in ARMS:
                step = next(s for s in gradings[(vid, arm)]["steps"] if s["step_id"] == sid)
                r = arms[arm]
                entry["arms"][arm] = {
                    "verdict": step["verdict"], "match": bool(r and r["match"]),
                    "time_ok": None if not r else r["time_match"],
                    "model_spans": r["model_spans"] if r else [],
                    "criteria": [{"id": c["criterion_id"], "text": texts.get(c["criterion_id"], ""),
                                  "verdict": c["verdict"], "source": c.get("source", ""),
                                  "explanation": c["explanation"]} for c in step["criteria"]]}
            bad = [n for n, x in entry["arms"].items() if not x["match"]]
            entry["group"] = (3 if not bad else 2 if len(bad) == len(ARMS) else 1)
            disputes.append(entry)
    disputes.sort(key=lambda d: (d["group"], d["video_id"], d["step"]))

    today = datetime.date.today().isoformat()
    data = {"title": f"#134 整支影片 vs 拆解：人工核對 {today}", "store": out.name, "rubric": rubric["version"],
            "videos": videos, "evidence": evidence, "disputes": disputes}
    out.write_text(TEMPLATE.replace("__TITLE__", data["title"]).replace(
        "__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")), encoding="utf-8")
    key_path = out.with_name(out.stem + "_key.json")
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    missing = [v for v, i in videos.items() if not i["src"]]
    print(f"{out}：說明層 {len(evidence)} 條、不一致 {len(disputes)} 步；對照表 {key_path}"
          + (f"；找不到影片：{'、'.join(missing)}" if missing else ""))
    print("抽樣池：", {k: v for k, v in key.items() if k.startswith("pool_")})
    return 0


TEMPLATE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
:root{--bg:#f6f6f4;--fg:#1f2328;--mute:#5d646d;--card:#fff;--line:#e0e0dc;--accent:#2f6f9f;--accbg:#e6f0f8;
--ok:#1f7a4d;--okbg:#e3f3ea;--bad:#b3261e;--badbg:#fbe7e5;--warn:#8a5a00;--warnbg:#fdf1d8}
@media (prefers-color-scheme: dark){:root{--bg:#161718;--fg:#e8e8e6;--mute:#a1a7ae;--card:#202224;--line:#36393c;
--accent:#7fb3dc;--accbg:#1f3042;--ok:#7fd1a4;--okbg:#1d3a2b;--bad:#f3a59f;--badbg:#45211e;--warn:#f0c46b;--warnbg:#3f3216}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 "Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif}
.wrap{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:16px;padding:12px 16px;max-width:1700px;margin:auto}
@media (max-width:900px){.wrap{grid-template-columns:1fr}.left{position:static!important}}
.left{position:sticky;top:12px;align-self:start}
.vbox{position:relative;overflow:hidden;background:#000;border-radius:8px;aspect-ratio:16/9}
video{width:100%;height:100%;display:block;transform-origin:50% 50%;transition:transform .15s}
.pick{position:absolute;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;gap:8px;color:#eee;padding:16px;text-align:center}
.pick.show{display:flex}
.ctl{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0}
button,select,textarea{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:4px 10px}
button{cursor:pointer}button:hover{border-color:var(--accent)}button.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
.now{font-variant-numeric:tabular-nums;font-weight:700;font-size:18px;margin-right:6px}
.help{font-size:13px;color:var(--mute)}
.top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
h1{font-size:19px;margin:0 8px 0 0}.prog{font-size:14px;color:var(--mute)}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}
.item.active{border-color:var(--accent);box-shadow:0 0 0 2px var(--accbg)}.item.done{opacity:.72}
.head{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}.head b{font-size:16px}
.lbl{font-size:12px;border-radius:4px;padding:0 6px;background:var(--accbg);color:var(--accent)}
.grp{font-size:13px;font-weight:700;border-radius:4px;padding:0 8px;background:var(--warnbg);color:var(--warn)}
.v{font-weight:700}.v.ok{color:var(--ok)}.v.bad{color:var(--bad)}
.crit{color:var(--mute);font-size:14px;margin:4px 0}
.exp{margin:6px 0;padding:6px 10px;border-left:3px solid var(--line);background:var(--bg);border-radius:4px}
.t{color:var(--accent);cursor:pointer;text-decoration:underline dotted;font-variant-numeric:tabular-nums}
.choices{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;align-items:center}
.arms{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:6px}
@media (max-width:1300px){.arms{grid-template-columns:1fr}}
.arm{border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:14px}
.arm .c{margin-top:4px}
.tag{font-size:12px;border-radius:4px;padding:0 5px}.tag.ok{background:var(--okbg);color:var(--ok)}.tag.bad{background:var(--badbg);color:var(--bad)}
textarea{width:100%;min-height:34px;margin-top:6px}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.note{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:14px}
</style></head><body>
<div class="wrap">
<div class="left">
  <div class="vbox"><video id="vid" preload="metadata" controls></video>
    <div class="pick" id="pick"><div>找不到這支影片，請選擇本機的原始影片檔</div><input type="file" id="file" accept="video/*"></div></div>
  <div class="ctl"><span class="now" id="now">00:00.0</span><span id="vidname" class="lbl"></span>
    <button data-d="-1">−1 s</button><button data-d="-0.1">−0.1 s</button><button data-d="0.1">+0.1 s</button><button data-d="1">+1 s</button>
    <button data-r="0.25">0.25×</button><button data-r="0.5">0.5×</button><button data-r="1" class="on">1×</button>
    <button id="zoom">放大 2×</button></div>
  <div class="help">點說明裡的時間會跳到那裡並開始播放。放大時點畫面可以換放大的位置。快捷鍵：空白 播放／暫停，← → 0.1 秒，Shift+← → 1 秒。</div>
</div>
<div>
  <div class="top"><h1 id="title"></h1><span class="prog" id="prog"></span>
    <button id="export">匯出 CSV</button></div>
  <div class="tabs"><button id="tab1" class="on">1. 說明層（遮組）</button><button id="tab2">2. 不一致案例</button></div>
  <div id="intro"></div>
  <div id="list"></div>
</div></div>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
</script>
</body></html>"""

TEMPLATE = TEMPLATE.replace("""<script>
const D""", """<script type="application/json" id="data">__DATA__</script>
<script>
const D""").replace("""const D = JSON.parse(document.getElementById("data").textContent);
</script>""", """const D = JSON.parse(document.getElementById("data").textContent);
__JS__
</script>""")

JS = r"""
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const mmss = t => { t = Math.max(0, t); const m = Math.floor(t / 60), s = t - m * 60; return `${String(m).padStart(2,"0")}:${s.toFixed(1).padStart(4,"0")}`; };
const STORE = "wv134:" + D.store;
// storage can be blocked (file:// or site data off): fall back to memory, export still works
let S = {};
try { S = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { S = {}; }
const save = () => { try { localStorage.setItem(STORE, JSON.stringify(S)); } catch (e) {} progress(); };
const vid = $("vid");
let cur = null, tab = 1, zoomed = false;
const blobs = {};

function load(v) {
  if (cur === v) return;
  cur = v; $("vidname").textContent = v;
  const src = blobs[v] || (D.videos[v] || {}).src || "";
  $("pick").classList.toggle("show", !src);
  if (src) vid.src = src; else vid.removeAttribute("src");
}
vid.addEventListener("error", () => $("pick").classList.add("show"));
$("file").addEventListener("change", e => { const f = e.target.files[0]; if (!f || !cur) return;
  blobs[cur] = URL.createObjectURL(f); $("pick").classList.remove("show"); vid.src = blobs[cur]; });
function seek(v, t) { load(v); const go = () => { vid.currentTime = t; vid.play().catch(() => {}); };
  if (vid.readyState >= 1) go(); else vid.addEventListener("loadedmetadata", go, {once: true}); }
setInterval(() => { $("now").textContent = mmss(vid.currentTime || 0); }, 100);
document.querySelectorAll("[data-d]").forEach(b => b.onclick = () => { vid.pause(); vid.currentTime += +b.dataset.d; });
document.querySelectorAll("[data-r]").forEach(b => b.onclick = () => { vid.playbackRate = +b.dataset.r;
  document.querySelectorAll("[data-r]").forEach(x => x.classList.toggle("on", x === b)); });
$("zoom").onclick = () => { zoomed = !zoomed; vid.style.transform = zoomed ? "scale(2)" : ""; $("zoom").classList.toggle("on", zoomed); };
vid.addEventListener("click", e => { if (!zoomed) return; e.preventDefault();
  const r = vid.getBoundingClientRect(); vid.style.transformOrigin = `${(e.clientX - r.left) / r.width * 100}% ${(e.clientY - r.top) / r.height * 100}%`; });
document.addEventListener("keydown", e => {
  if (["TEXTAREA", "SELECT", "INPUT"].includes(e.target.tagName)) return;
  if (e.key === " ") { e.preventDefault(); vid.paused ? vid.play() : vid.pause(); }
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); vid.pause();
    vid.currentTime += (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 1 : 0.1); }
});

// mm:ss(.s) and mm:ss–mm:ss in text become links
function linkify(text, v) {
  return esc(text).replace(/(\d{2}):(\d{2}(?:\.\d+)?)/g, (m, a, b) =>
    `<span class="t" data-v="${v}" data-t="${(+a) * 60 + (+b)}">${m}</span>`);
}
document.addEventListener("click", e => { const t = e.target.closest(".t"); if (t) seek(t.dataset.v, +t.dataset.t); });
const spans = (sp, v) => (sp || []).map(([a, b]) => `<span class="t" data-v="${v}" data-t="${a}">${mmss(a)}</span>–${mmss(b)}`).join("、") || "無";
const vcls = v => v === "正確" ? "ok" : (v === "錯誤" || v === "部分正確") ? "bad" : "";

const EVID = ["正確", "時間不對", "內容不符", "無法確認"];
const VERD = ["正確", "錯誤", "部分正確", "無法判定", "待老師確認"];
const CAUSE = ["拆解組看錯或漏看", "整支組看錯或漏看", "整支組描述了沒發生的動作（幻覺）", "時間對不上（切分或引用時間錯）",
               "人工判定可能錯", "標準不清楚或理解不同", "畫面看不到"];
const doneE = e => !!(S[e.key] && S[e.key].evidence);
const doneD = d => !!(S[d.key] && S[d.key].verdict);
const part1Done = () => D.evidence.every(doneE);

function progress() {
  const a = D.evidence.filter(doneE).length, b = D.disputes.filter(doneD).length;
  $("prog").textContent = `說明層 ${a}/${D.evidence.length}　不一致 ${b}/${D.disputes.length}　rubric ${D.rubric}`;
}
function hum(h, v) {
  if (!h) return "人工：無";
  return `人工：<span class="v ${vcls(h.verdict)}">${esc(h.verdict)}</span>　時間 ${spans(h.spans, v)}` +
    (h.why ? `　<span class="help">${esc(h.why)}</span>` : "");
}
function btns(key, field, opts) {
  const val = (S[key] || {})[field];
  return opts.map(o => `<button data-k="${key}" data-f="${field}" data-o="${esc(o)}" class="${val === o ? "on" : ""}">${esc(o)}</button>`).join("");
}
function checks(key, opts) {
  const val = (S[key] || {}).causes || [];
  return opts.map(o => `<button data-k="${key}" data-c="${esc(o)}" class="${val.includes(o) ? "on" : ""}">${esc(o)}</button>`).join("");
}
function note(key) { return `<textarea data-k="${key}" placeholder="備註（選填）">${esc((S[key] || {}).note || "")}</textarea>`; }

function render() {
  $("tab1").classList.toggle("on", tab === 1); $("tab2").classList.toggle("on", tab === 2);
  const L = [];
  if (tab === 1) {
    $("intro").innerHTML = `<div class="note">遮組核對：A／B 是兩種評分方式，每支影片隨機分配，同一支影片內同一字母是同一組。說明裡的工具紀錄已拿掉。
      這些步驟的模型判定都和人工一致，要看的是<b>說明與時間是不是真的對</b>：<br>
      <b>正確</b>＝描述的動作在那個時間確實發生；<b>時間不對</b>＝動作有發生，但引用的時間不是那裡；<b>內容不符</b>＝描述的動作或理由和畫面不符（判定剛好對）；<b>無法確認</b>＝畫面看不清楚。</div>`;
    for (const e of D.evidence) {
      L.push(`<div class="item ${doneE(e) ? "done" : ""}" data-v="${e.video_id}">
        <div class="head"><b>${esc(e.video_id)}　步驟 ${e.step}</b><span class="lbl">${esc(e.criterion)}</span><span class="grp">組 ${esc(e.group)}</span>
          ${e.short ? '<span class="lbl">短動作</span>' : ""}<span class="help">${esc(e.name)}</span></div>
        <div class="crit">${esc(e.text)}</div>
        <div>模型：<span class="v ${vcls(e.verdict)}">${esc(e.verdict)}</span>　時間 ${spans(e.times, e.video_id)}</div>
        <div class="exp">${linkify(e.explanation, e.video_id)}</div>
        <div class="help">${hum(e.human, e.video_id)}</div>
        <div class="choices">證據：${btns(e.key, "evidence", EVID)}</div>${note(e.key)}</div>`);
    }
  } else {
    $("intro").innerHTML = part1Done() || S._unlock ? `<div class="note">不遮組。每一步是三組裡至少一組和人工不一致（判定不同，或判定相同但時間對不上）。
      請看影片後選<b>核對後的判定</b>，再勾選原因（可複選）。</div>` :
      `<div class="note">請先完成第 1 部分（說明層）。這裡會顯示每一組的原文，先看會破壞遮組。
       <button id="unlock">仍要先看（之後的說明層不算遮組）</button></div>`;
    const GH = {1: "一、兩組結果不同（至少一組判定和人工一致、至少一組不同）", 2: "二、三組判定都和人工不同",
                3: "三、三組判定都和人工一致，只有時間對不上"};
    let lastG = 0;
    if (part1Done() || S._unlock) for (const d of D.disputes) {
      if (d.group !== lastG) { lastG = d.group; L.push(`<h2 style="font-size:16px;margin:16px 0 8px">${GH[d.group]}（${D.disputes.filter(x => x.group === d.group).length} 步）</h2>`); }
      const arms = Object.entries(d.arms).map(([n, a]) => `<div class="arm"><b>${esc(n)}</b>　<span class="v ${vcls(a.verdict)}">${esc(a.verdict)}</span>
          <span class="tag ${a.match ? "ok" : "bad"}">${a.match ? "判定一致" : "判定不同"}</span>
          ${a.time_ok === false ? '<span class="tag bad">時間對不上</span>' : ""}
          <div class="help">比對時間 ${spans(a.model_spans, d.video_id)}</div>
          ${a.criteria.map(c => `<div class="c"><span class="lbl">${esc(c.id)}</span> <span class="v ${vcls(c.verdict)}">${esc(c.verdict)}</span>
            <span class="help">${esc(c.source)}</span><div>${linkify(c.explanation, d.video_id)}</div></div>`).join("")}</div>`).join("");
      L.push(`<div class="item ${doneD(d) ? "done" : ""}" data-v="${d.video_id}">
        <div class="head"><b>${esc(d.video_id)}　步驟 ${d.step}</b><span class="help">${esc(d.name)}</span></div>
        <div>${hum(d.human, d.video_id)}</div><div class="arms">${arms}</div>
        <div class="choices">核對後判定：${btns(d.key, "verdict", VERD)}</div>
        <div class="choices">原因：${checks(d.key, CAUSE)}</div>${note(d.key)}</div>`);
    }
  }
  $("list").innerHTML = L.join("");
  const u = $("unlock"); if (u) u.onclick = () => { S._unlock = new Date().toISOString(); save(); render(); };
  progress();
}
$("list").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b || !b.dataset.k) return;
  const k = b.dataset.k; S[k] = S[k] || {};
  if (b.dataset.f) S[k][b.dataset.f] = S[k][b.dataset.f] === b.dataset.o ? undefined : b.dataset.o;
  if (b.dataset.c) { const c = new Set(S[k].causes || []); c.has(b.dataset.c) ? c.delete(b.dataset.c) : c.add(b.dataset.c); S[k].causes = [...c]; }
  S[k].at = new Date().toISOString(); save(); render();
});
$("list").addEventListener("input", e => { const t = e.target; if (t.tagName !== "TEXTAREA") return;
  S[t.dataset.k] = S[t.dataset.k] || {}; S[t.dataset.k].note = t.value; save(); });
$("list").addEventListener("focusin", e => { const it = e.target.closest(".item"); if (it) load(it.dataset.v); });
$("list").addEventListener("mouseover", e => { const it = e.target.closest(".item"); if (!it) return;
  document.querySelectorAll(".item.active").forEach(x => x.classList.remove("active")); it.classList.add("active"); });
$("tab1").onclick = () => { tab = 1; render(); };
$("tab2").onclick = () => { tab = 2; render(); };
$("export").onclick = () => {
  const q = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [["部分", "key", "影片ID", "步驟", "標準", "組", "模型判定", "人工判定", "證據", "核對後判定", "原因", "備註", "時間", "說明層前已看不一致"]];
  for (const e of D.evidence) { const s = S[e.key] || {};
    rows.push(["說明層", e.key, e.video_id, e.step, e.criterion, e.group, e.verdict, (e.human || {}).verdict, s.evidence, "", "", s.note, s.at,
      S._unlock && s.at && s.at > S._unlock ? "是" : ""]); }
  for (const d of D.disputes) { const s = S[d.key] || {};
    rows.push(["不一致", d.key, d.video_id, d.step, "", "", Object.entries(d.arms).map(([n, a]) => `${n}:${a.verdict}`).join(" "),
      (d.human || {}).verdict, "", s.verdict, (s.causes || []).join("；"), s.note, s.at, ""]); }
  const blob = new Blob(["﻿" + rows.map(r => r.map(q).join(",")).join("\r\n")], {type: "text/csv"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = D.store.replace(/\.html$/, "") + "_結果.csv"; a.click();
};
$("title").textContent = D.title;
if (D.evidence.length) load(D.evidence[0].video_id);
render();
"""
TEMPLATE = TEMPLATE.replace("__JS__", JS)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
