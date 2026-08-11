"""Agent-2 网页版:主界面列出所有案子,点进某个案子看报告+聊深挖对话;也可以直接在网页上
新建案子——上传卷宗 PDF(可以带密码),提交后台跑完整的摄取+分析流程
(`cli.run_case.run_case`),跑完自动出现在案子列表里。

起诉书/起诉意见书不需要律师上传时提前单独分开上传——现实中卷宗材料经常是文书和证据混在
一起。摄取完成后系统会自动扫一遍标题特征,找出疑似起诉书/起诉意见书的候选页码范围,律师
在案子页面上确认(可以调整页码范围)之后才会真正生成对照表,详见 cli/run_case.py 顶部说明。

之前是每次起服务都要在命令行指定一个案子目录(`python -m web.app data/cases/<案子>`),
律师自己没法用——不会开终端、不知道要往哪塞 PDF。现在服务只启动一次(`python -m web.app`),
主页是案子列表,新建案子这件事也在网页上完成,不用再经过开发者手工跑 CLI。

后台任务跟踪:新建案子提交后开一个后台线程跑 `run_case`,进度通过内存里的 `_JOBS` 字典
(状态:running/done/error)轮询获取,不落盘、不做持久化任务队列——这是给单个律师本地自用的
工具,不需要扛得住服务重启后还能恢复进度这种程度的可靠性,过度设计了后续维护成本更高。

对话逻辑(不管是终端版还是网页版这个模块)复用 analysis/deep_dive_core.py。前端故意用最简单
的原生 HTML/JS,不引入构建工具链——这个项目里最重要的是后端交互逻辑本身,前端只要能稳定跑
起来就够了。
"""

import json
import os
import shutil
import threading
import time

from flask import Flask, jsonify, redirect, render_template, request

from analysis.deep_dive_core import DeepDiveCase, confirm_finding, process_turn
from analysis.indictment_check import load_annotations, parse_comparison_groups, save_annotation
from cli.run_case import confirm_indictment, dismiss_indictment_candidate, run_case

app = Flask(__name__)

CASES_DIR = "data/cases"
RAW_DIR = "data/raw"
LEDGER_DIR = "data/ledger"
CONFIGS_DIR = "configs"

_case_cache: dict[str, DeepDiveCase] = {}
_JOBS: dict[str, dict] = {}  # case_name -> {"status": "running"/"done"/"error", "started_at": float, "error": str|None}


def _sanitize_name(name: str) -> str:
    """案名/卷标签会直接拼进文件路径,只挡掉真正会破坏路径结构的字符(斜杠、上级目录引用),
    中文字符正常保留——不能用 werkzeug 的 secure_filename,它会把中文整段砍掉。"""
    name = name.strip().replace("..", "")
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "")
    return name


def get_case(case_name: str, fresh: bool = False) -> DeepDiveCase:
    """fresh=True 强制重新从磁盘加载,不用缓存——确认/忽略起诉书候选之后 manifest.json
    变了,缓存里的旧 DeepDiveCase 不会自动感知,调用方需要显式要求刷新。"""
    if fresh or case_name not in _case_cache:
        _case_cache[case_name] = DeepDiveCase(os.path.join(CASES_DIR, case_name))
    return _case_cache[case_name]


def list_cases() -> list[dict]:
    """列出所有案子:优先读 manifest.json(已经跑完的);manifest 还没写出来但这次服务运行期间
    发起过任务的,从内存里的 _JOBS 补一条"处理中"的记录——服务重启后如果任务还没跑完,这条
    内存记录会丢,列表里会暂时看不到它,直到它写出 manifest.json(见模块开头说明,这个折中
    是有意的,单人本地自用不值得为这种边缘情况上持久化任务队列)。"""
    cases = []
    seen = set()
    if os.path.isdir(CASES_DIR):
        for name in sorted(os.listdir(CASES_DIR)):
            manifest_path = os.path.join(CASES_DIR, name, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            cases.append(
                {
                    "case_name": name,
                    "status": "error" if not manifest.get("ok") else ("healthy" if manifest.get("healthy") else "unhealthy"),
                    "created_at": manifest.get("created_at"),
                    "batch_count": manifest.get("batch_count"),
                    "pending_candidates": len(manifest.get("indictment_candidates", [])),
                }
            )
            seen.add(name)
            job = _JOBS.get(name)
            if job and job["status"] == "running":
                # 已经写出 manifest 了,内存里那条"处理中"状态是旧的。
                job["status"] = "done"

    for name, job in _JOBS.items():
        if name in seen:
            continue
        cases.append(
            {
                "case_name": name,
                "status": job["status"],  # running / error(还没来得及写 manifest 就失败了)
                "created_at": None,
                "error": job.get("error"),
                "phase": job.get("phase"),
                "elapsed_seconds": round(time.time() - job["started_at"]),
            }
        )

    cases.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return cases


def _run_new_case_job(case_name: str, volumes: list[dict]) -> None:
    _JOBS[case_name] = {
        "status": "running",
        "started_at": time.time(),
        "error": None,
        "phase": "准备中",
        "phase_updated_at": time.time(),
    }

    def on_progress(msg: str) -> None:
        _JOBS[case_name]["phase"] = msg
        _JOBS[case_name]["phase_updated_at"] = time.time()

    try:
        config = {"case_name": case_name, "volumes": volumes, "existing_ledgers": []}
        os.makedirs(CONFIGS_DIR, exist_ok=True)
        config_path = os.path.join(CONFIGS_DIR, f"{case_name}.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        run_case(config_path, out_dir=CASES_DIR, on_progress=on_progress)
        _JOBS[case_name]["status"] = "done"
    except Exception as e:
        _JOBS[case_name]["status"] = "error"
        _JOBS[case_name]["error"] = f"{type(e).__name__}: {e}"


def delete_case(case_name: str) -> None:
    """删掉一个案子在这个案子专属目录下的一切(data/cases/<案子>/、data/raw/<案子>/、
    configs/<案子>.json)——不碰 data/ledger/ 顶层目录。旧格式案子(通过 CLI 的
    existing_ledgers 手动指定台账路径,比如某个已经提前摄取好的老案子)可能引用的是 data/ledger/ 下的
    台账文件,那些台账是真金白银跑视觉识别出来的、可能被好几个案子共用/引用,删这个案子
    不该连累到它们——只删案子目录里明确是这个案子专属产出的东西,案子目录之外一律不碰,
    宁可删得不够干净,也不要误删别的案子还在用的原始数据。"""
    for path in (os.path.join(CASES_DIR, case_name), os.path.join(RAW_DIR, case_name)):
        if os.path.isdir(path):
            shutil.rmtree(path)
    config_path = os.path.join(CONFIGS_DIR, f"{case_name}.json")
    if os.path.isfile(config_path):
        os.remove(config_path)
    _case_cache.pop(case_name, None)
    _JOBS.pop(case_name, None)


@app.route("/")
def home():
    return render_template("home.html", cases=list_cases())


@app.route("/new-case")
def new_case_form():
    return render_template("new_case.html")


@app.route("/api/new-case", methods=["POST"])
def create_new_case():
    case_name = _sanitize_name(request.form.get("case_name", ""))
    if not case_name:
        return jsonify({"error": "案名不能为空"}), 400
    if os.path.isdir(os.path.join(CASES_DIR, case_name)) or case_name in _JOBS:
        return jsonify({"error": f"案子 “{case_name}” 已经存在,换一个名字(或者去案子列表里直接打开它)"}), 400

    labels = request.form.getlist("volume_label")
    passwords = request.form.getlist("volume_password")
    files = request.files.getlist("volume_pdf")
    if not files or not any(f.filename for f in files):
        return jsonify({"error": "至少要上传一份卷宗 PDF"}), 400

    raw_dir = os.path.join(RAW_DIR, case_name)
    os.makedirs(raw_dir, exist_ok=True)

    volumes = []
    for i, f in enumerate(files):
        if not f.filename:
            continue
        label = _sanitize_name(labels[i]) if i < len(labels) and labels[i].strip() else f"卷{i + 1}"
        password = passwords[i].strip() if i < len(passwords) and passwords[i].strip() else None
        dest = os.path.join(raw_dir, f"{label}.pdf")
        f.save(dest)
        v = {"pdf": dest, "label": label}
        if password:
            v["password"] = password
        volumes.append(v)

    thread = threading.Thread(target=_run_new_case_job, args=(case_name, volumes), daemon=True)
    thread.start()

    return redirect(f"/case-status/{case_name}")


@app.route("/case-status/<case_name>")
def case_status_page(case_name):
    return render_template("case_status.html", case_name=case_name)


@app.route("/api/case/<case_name>/delete", methods=["POST"])
def delete_case_route(case_name):
    if not os.path.isdir(os.path.join(CASES_DIR, case_name)) and not os.path.isdir(os.path.join(RAW_DIR, case_name)):
        return jsonify({"error": "这个案子不存在(可能已经删过了)"}), 404
    delete_case(case_name)
    return jsonify({"ok": True})


@app.route("/api/case-status/<case_name>")
def case_status_api(case_name):
    manifest_path = os.path.join(CASES_DIR, case_name, "manifest.json")
    if os.path.isfile(manifest_path):
        return jsonify({"status": "done"})
    job = _JOBS.get(case_name)
    if not job:
        return jsonify({"status": "unknown", "error": "没有找到这个案子的处理记录(服务是不是重启过?)"}), 404
    return jsonify(
        {
            "status": job["status"],
            "elapsed_seconds": round(time.time() - job["started_at"]),
            "phase": job.get("phase"),
            "seconds_since_phase_update": round(time.time() - job["phase_updated_at"]) if job.get("phase_updated_at") else None,
            "error": job.get("error"),
        }
    )


@app.route("/case/<case_name>")
def case_chat_page(case_name):
    return render_template("index.html", case_name=case_name)


@app.route("/case/<case_name>/timeline")
def timeline_page(case_name):
    case = get_case(case_name)
    if not case.timeline_path or not os.path.isfile(case.timeline_path):
        return render_template("timeline.html", case_name=case_name, timeline_text=None)
    with open(case.timeline_path, "r", encoding="utf-8") as f:
        timeline_text = f.read()
    return render_template("timeline.html", case_name=case_name, timeline_text=timeline_text)


@app.route("/case/<case_name>/api/case")
def get_case_api(case_name):
    case = get_case(case_name)
    return jsonify(
        {
            "case_name": case.manifest["case_name"],
            "report": case.report,
            "history": case.load_history(),
            "indictments": [{"type": i["type"], "healthy": i.get("healthy")} for i in case.indictments],
            "indictment_candidates": case.indictment_candidates,
        }
    )


@app.route("/case/<case_name>/indictment-candidates")
def indictment_candidates_page(case_name):
    return render_template("indictment_candidates.html", case_name=case_name)


@app.route("/case/<case_name>/api/chat", methods=["POST"])
def chat(case_name):
    case = get_case(case_name)
    user_input = (request.json or {}).get("message", "").strip()
    if not user_input:
        return jsonify({"error": "消息不能为空"}), 400

    history = case.load_history()
    reply, tool_events, pending_proposals = process_turn(case, history, user_input)
    return jsonify({"reply": reply, "tool_events": tool_events, "pending_proposals": pending_proposals})


@app.route("/case/<case_name>/api/chat/confirm-finding", methods=["POST"])
def confirm_finding_route(case_name):
    # 硬接口的真正落点:这个接口只在律师点了"确认"或"推翻"按钮时才会被调用,是这一轮对话
    # 里唯一真正写入 verification_log.json 的入口——模型自己永远走不到这里。
    case = get_case(case_name)
    body = request.json or {}
    proposal = body.get("proposal")
    status = body.get("status")
    if not proposal or status not in ("confirmed", "overturned"):
        return jsonify({"error": "参数不对:需要 proposal 和 status(confirmed/overturned)"}), 400

    entry = confirm_finding(case, proposal, status)
    return jsonify({"ok": True, "entry": entry})


@app.route("/case/<case_name>/api/indictment-candidates/confirm", methods=["POST"])
def confirm_indictment_candidate_route(case_name):
    # 硬接口:候选是正则扫出来的,永远只是"疑似"——只有律师点了这个接口(确认页码范围没问题,
    # 必要时先手动调整过)才会真正生成对照表,系统自己不会替律师认定"这就是起诉书"。
    body = request.json or {}
    doc_type = body.get("type")
    ledger_path = body.get("ledger_path")
    start_page = body.get("start_page")
    end_page = body.get("end_page")
    # 候选原始范围:律师可能已经手动调整过 start_page/end_page,这两个字段专门用来在候选
    # 列表里精确定位、摘掉对应的那一条(见 confirm_indictment 的说明)。前端没传就退化成
    # 直接用 start_page/end_page 去匹配。
    candidate_start_page = body.get("candidate_start_page", start_page)
    candidate_end_page = body.get("candidate_end_page", end_page)
    if not all([doc_type, ledger_path]) or start_page is None or end_page is None:
        return jsonify({"error": "参数不对:需要 type、ledger_path、start_page、end_page"}), 400
    try:
        entry = confirm_indictment(
            os.path.join(CASES_DIR, case_name),
            doc_type,
            ledger_path,
            int(start_page),
            int(end_page),
            candidate_start_page=int(candidate_start_page),
            candidate_end_page=int(candidate_end_page),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    get_case(case_name, fresh=True)  # manifest 变了,刷新缓存,不然这个进程里之后看到的还是旧数据
    return jsonify({"ok": True, "entry": entry})


@app.route("/case/<case_name>/api/indictment-candidates/dismiss", methods=["POST"])
def dismiss_indictment_candidate_route(case_name):
    body = request.json or {}
    ledger_path = body.get("ledger_path")
    start_page = body.get("start_page")
    end_page = body.get("end_page")
    if not ledger_path or start_page is None or end_page is None:
        return jsonify({"error": "参数不对:需要 ledger_path、start_page、end_page"}), 400
    dismiss_indictment_candidate(os.path.join(CASES_DIR, case_name), ledger_path, int(start_page), int(end_page))
    get_case(case_name, fresh=True)
    return jsonify({"ok": True})


@app.route("/case/<case_name>/indictment/<doc_type>")
def indictment_page(case_name, doc_type):
    return render_template("indictment.html", case_name=case_name, doc_type=doc_type)


@app.route("/case/<case_name>/api/indictment/<doc_type>")
def get_indictment(case_name, doc_type):
    case = get_case(case_name)
    matches = [i for i in case.indictments if i["type"] == doc_type and i.get("comparison_path")]
    if not matches:
        return jsonify({"available": False})

    # 同一类型理论上可能被确认多次(比如两名被告人各有一份起诉书),这里全部拼起来按顺序展示,
    # 备注仍然按分组下标存,下标是跨这几份合并之后的连续序号,不是每份各自归零。
    annotations = load_annotations(case.indictment_annotations_path(doc_type))
    all_groups = []
    for m in matches:
        with open(m["comparison_path"], "r", encoding="utf-8") as f:
            comparison_text = f.read()
        all_groups.extend(parse_comparison_groups(comparison_text, doc_type=doc_type))

    return jsonify(
        {
            "available": True,
            "case_name": case.manifest["case_name"],
            "doc_type": doc_type,
            "groups": [
                {
                    "index": i,
                    "indictment": g["indictment"],
                    "evidence": g["evidence"],
                    "annotation": annotations.get(i),
                }
                for i, g in enumerate(all_groups)
            ],
        }
    )


@app.route("/case/<case_name>/api/indictment/<doc_type>/annotate", methods=["POST"])
def annotate_indictment(case_name, doc_type):
    case = get_case(case_name)
    body = request.json or {}
    group_index = body.get("group_index")
    note = (body.get("note") or "").strip()
    if group_index is None:
        return jsonify({"error": "缺少 group_index"}), 400

    entry = save_annotation(case.indictment_annotations_path(doc_type), int(group_index), note)
    return jsonify({"ok": True, "annotation": entry})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent-2 网页版:主界面案子列表 + 新建案子 + 深挖对话")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    os.makedirs(CASES_DIR, exist_ok=True)
    print(f"打开 http://127.0.0.1:{args.port}")
    app.run(debug=False, port=args.port)
