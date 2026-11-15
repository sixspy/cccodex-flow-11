from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from sanic import Request, Sanic, json, redirect
from sanic.response import file

from app.config import Config
from app.db import (
    get_db,
    get_flow,
    get_merge,
    init_db,
    list_flows,
    list_merges,
    run_extract,
    run_land,
    run_merge,
    run_merge_land,
    run_merge_segment,
    update_merge_policy,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ser = URLSafeSerializer(Config.SECRET, salt="cc-flow-session")

app = Sanic("cc-flow-lab")
app.static("/static", str(STATIC), name="static")


def _user(request: Request) -> str | None:
    raw = request.cookies.get("flow_session")
    if not raw:
        return None
    try:
        data = ser.loads(raw)
        u = data.get("user") if isinstance(data, dict) else None
        return u if u == Config.ADMIN_USER else None
    except BadSignature:
        return None


@app.listener("before_server_start")
async def _boot(_app, _loop):
    init_db()


@app.get("/")
async def root(request: Request):
    return redirect("/app/" if _user(request) else "/login")


@app.get("/login")
async def login_page(request: Request):
    return await file(STATIC / "login.html")


@app.get("/app/", name="spa_root")
async def spa_root(request: Request):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.get("/app/<path:path>", name="spa_path")
async def spa_path(request: Request, path: str = ""):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = request.json or {}
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json({"ok": False, "error": "账号或密码错误"}, status=400)
    resp = json({"ok": True, "user": user})
    resp.add_cookie(
        "flow_session",
        ser.dumps({"user": user}),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    resp = json({"ok": True})
    resp.delete_cookie("flow_session", path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user = _user(request)
    if not user:
        return json({"detail": "未登录"}, status=401)
    return json({"user": user, "lab": Config.LAB_NAME, "domain": "数据工程"})


@app.get("/api/flows")
async def api_flows(request: Request):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        return json({"items": list_flows(conn)})


@app.get("/api/flows/<flow_id:int>")
async def api_flow(request: Request, flow_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = get_flow(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<flow_id:int>/extract")
async def api_extract(request: Request, flow_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = run_extract(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<flow_id:int>/land")
async def api_land(request: Request, flow_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = run_land(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.get("/api/merges")
async def api_merges(request: Request):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        return json({"items": list_merges(conn)})


@app.get("/api/merges/<merge_id:int>")
async def api_merge(request: Request, merge_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = get_merge(conn, merge_id)
    if not item:
        return json({"detail": "合并不存在"}, status=404)
    return json(item)


@app.patch("/api/merges/<merge_id:int>/policy")
async def api_merge_policy(request: Request, merge_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    body = request.json or {}
    with get_db() as conn:
        item = update_merge_policy(conn, merge_id, body.get("conflict_policy"))
    return _merge_result(item)


@app.post("/api/merges/<merge_id:int>/merge")
async def api_merge_run(request: Request, merge_id: int):
    user = _user(request)
    if not user:
        return json({"detail": "未登录"}, status=401)
    body = request.json or {}
    with get_db() as conn:
        item = run_merge(conn, merge_id, body.get("disposition"), actor=user)
    return _merge_result(item)


@app.post("/api/merges/<merge_id:int>/segment")
async def api_merge_segment(request: Request, merge_id: int):
    user = _user(request)
    if not user:
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = run_merge_segment(conn, merge_id, actor=user)
    return _merge_result(item)


@app.post("/api/merges/<merge_id:int>/land")
async def api_merge_land(request: Request, merge_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = run_merge_land(conn, merge_id)
    return _merge_result(item)


def _merge_result(item):
    if item is None:
        return json({"detail": "合并不存在"}, status=404)
    if isinstance(item, dict) and item.get("error"):
        return json(
            {"detail": item["detail"], "code": item["code"]}, status=item["status"]
        )
    # 动作接口统一直接返回合并详情，与 GET /api/merges/<id> 同构
    return json(item.get("item") if isinstance(item, dict) and "item" in item else item)

