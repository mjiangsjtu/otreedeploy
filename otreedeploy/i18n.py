"""Bilingual UI: English and Simplified Chinese.

Deliberately a plain dictionary rather than gettext. The catalogue is small, it lives in
one file a bilingual colleague can edit without a toolchain, and there is no compile step
to forget before a deploy. English source text is the key, so an untranslated string
degrades to English instead of showing a marker.

Scope is the interface: navigation, headings, buttons, labels, status and notices.
Build logs and the live journal are passed through untranslated -- they are uv's, oTree's
and systemd's own words, and translating them would misrepresent what the tools said.
"""

from __future__ import annotations

import time

DEFAULT = "en"

# Value is the name shown in the switcher, always written in its own language.
LANGUAGES = {"en": "English", "zh": "简体中文"}

# What goes in <html lang="...">; "zh" alone does not say which script.
HTML_LANG = {"en": "en", "zh": "zh-Hans"}

COOKIE = "otreedeploy_lang"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365


ZH: dict[str, str] = {
    # -- chrome ------------------------------------------------------------------
    "Projects": "实验项目",
    "Upload": "上传",
    "System": "系统",
    "Language": "语言",
    # -- statuses and badges -----------------------------------------------------
    "running": "运行中",
    "enabled": "已启用",
    "stopped": "已停止",
    "current": "当前",
    "ok": "成功",
    "failed": "失败",
    # -- index -------------------------------------------------------------------
    "Experiments": "实验",
    "is live on port {port}.": "正在端口 {port} 上提供服务。",
    "Nothing is currently serving on port {port}.": "目前没有实验在端口 {port} 上提供服务。",
    "release {release}": "版本 {release}",
    "{n} session": "{n} 个会话",
    "{n} sessions": "{n} 个会话",
    "{n} active": "{n} 人进行中",
    "port {port}": "端口 {port}",
    "Activate": "启用",
    "No experiments yet.": "还没有实验。",
    "Upload an {file} file to get started.": "上传一个 {file} 文件即可开始。",
    "Upload an experiment": "上传实验",
    "Recent activity": "近期活动",
    # -- upload ------------------------------------------------------------------
    "Upload a new version of {name}": "上传 {name} 的新版本",
    "Your collected data will be kept. Only the code is replaced.": "已收集的数据会保留，只替换代码。",
    "Select the {file} file produced by {cmd}.": "选择由 {cmd} 生成的 {file} 文件。",
    "Drop your .otreezip here": "把 .otreezip 文件拖到这里",
    "or click to choose a file": "或点击选择文件",
    "Deploy": "部署",
    "Cancel": "取消",
    "Uploading…": "正在上传…",
    "The upload is checked, its dependencies installed, and the experiment test-started on a private port before anything reaches port {port}. If it fails to start, whatever is currently live keeps running.": "上传的文件会先经过检查、安装依赖，并在一个私有端口上试运行，之后才会接触端口 {port}。如果启动失败，当前正在运行的实验不受影响。",
    # -- job ---------------------------------------------------------------------
    "Working…": "正在处理…",
    "Go to the project": "前往该实验",
    "All experiments": "所有实验",
    "Finished successfully.": "已成功完成。",
    "Stopped to protect a session in progress.": "为保护正在进行的会话已中止。",
    "This deploy failed. Nothing was changed for participants.": "本次部署失败，参与者一侧没有任何改动。",
    # -- project -----------------------------------------------------------------
    "{n} participant is active right now.": "当前有 {n} 名参与者正在进行实验。",
    "{n} participants are active right now.": "当前有 {n} 名参与者正在进行实验。",
    "Actions that would interrupt them are blocked; each one offers an override if you really mean it.": "会打断他们的操作已被阻止；如果确实需要，每项操作都提供了强制执行的选项。",
    "Make this live on port {port}": "在端口 {port} 上启用",
    "Activate anyway": "仍然启用",
    "Open oTree admin ↗": "打开 oTree 管理页 ↗",
    "Restart": "重启",
    "Stop": "停止",
    "Upload new version": "上传新版本",
    "Data & downloads": "数据与下载",
    "Participants use {url}.": "参与者请访问 {url}。",
    "The oTree admin username is {user}; the password is below.": "oTree 管理员用户名为 {user}，密码见下方。",
    "Settings": "设置",
    "Display name": "显示名称",
    "oTree admin password": "oTree 管理员密码",
    "Username is always {user}.": "用户名固定为 {user}。",
    "Access level": "访问级别",
    "STUDY — participants need a link; admin needs the password": "STUDY — 参与者需要链接，管理页需要密码",
    "DEMO — the demo page is open to anyone": "DEMO — 演示页面对所有人开放",
    "Save": "保存",
    "Saving restarts the experiment if it is running.": "如果实验正在运行，保存后会重启。",
    "Status": "状态",
    "Sessions": "会话数",
    "Database": "数据库",
    "Data tables": "数据表",
    "created": "已创建",
    "not yet created": "尚未创建",
    "Created": "创建时间",
    "Backups": "备份",
    "Download": "下载",
    "Releases": "版本",
    "Release": "版本",
    "Uploaded file": "上传的文件",
    "Result": "结果",
    "Roll back to this": "回滚到此版本",
    "No releases yet.": "还没有版本。",
    "Live log": "实时日志",
    "Connecting to the log…": "正在连接日志…",
    "Streamed from the system journal for this experiment.": "来自该实验的系统日志实时输出。",
    "(log stream disconnected — reload to reconnect)": "（日志连接已断开 — 刷新页面可重新连接）",
    "Danger zone": "危险操作",
    "Reset the database or delete this experiment": "重置数据库或删除该实验",
    "Reset the database.": "重置数据库。",
    "Deletes every session and all collected data for this experiment. A backup is saved first, and appears above.": "会删除该实验的所有会话和已收集的数据。系统会先保存一份备份，显示在上方。",
    "Do this when you have changed your models and the experiment no longer starts.": "当你修改了模型字段、导致实验无法启动时，使用此功能。",
    "Type {slug} to confirm": "输入 {slug} 以确认",
    "Yes, even though {n} participant(s) are active": "确认执行，即使当前有 {n} 名参与者在进行实验",
    "Reset database": "重置数据库",
    "Delete this experiment.": "删除该实验。",
    "Removes the code, every release and the database. A final backup is kept.": "会删除代码、所有版本和数据库，但会保留最后一份备份。",
    "The backup file stays on the server, but this page will be gone.": "备份文件仍保留在服务器上，但此页面将不复存在。",
    "Delete experiment": "删除实验",
    "Delete all collected data for {slug}?": "确定删除 {slug} 的全部数据吗？",
    "Permanently delete {slug}?": "确定永久删除 {slug} 吗？",
    # -- data --------------------------------------------------------------------
    "Data": "数据",
    "This experiment is not currently running. Downloads still work — it is started privately for a moment to produce the file, without touching whatever is live on port {port}.": "该实验当前未在运行。下载仍然可用 — 系统会短暂地私下启动它来生成文件，不会影响端口 {port} 上正在运行的实验。",
    "No data yet.": "还没有数据。",
    "The database is ready. Once you create a session in the oTree admin and participants take part, their data appears here.": "数据库已就绪。在 oTree 管理页创建会话并有参与者参与后，数据会显示在这里。",
    "This experiment has not been started yet, so it has no database tables.": "该实验尚未启动过，因此还没有数据表。",
    "Everything, across all sessions": "全部会话的汇总数据",
    "All sessions, wide format (CSV)": "全部会话，宽表格式（CSV）",
    "Page times (CSV)": "页面停留时间（CSV）",
    "Chat messages (CSV)": "聊天记录（CSV）",
    "“Wide format” is the one-row-per-participant file most analyses start from — the same file oTree’s own data page produces.": "“宽表格式”是每位参与者一行的文件，多数分析都从它开始 — 与 oTree 自带数据页生成的文件相同。",
    "By session": "按会话",
    "Session": "会话",
    "Label": "标签",
    "Participants": "参与者",
    "Download CSV": "下载 CSV",
    # -- system ------------------------------------------------------------------
    "Health of the machine this lab server runs on.": "本实验室服务器所在主机的运行状况。",
    "Dry run.": "试运行模式。",
    "Experiments use SQLite instead of PostgreSQL and systemd is simulated. Useful for trying things out; not how the lab server should run.": "实验使用 SQLite 而非 PostgreSQL，systemd 也是模拟的。适合试用，但不应以此方式运行正式的实验室服务器。",
    "Port {port}": "端口 {port}",
    "{slug} is the live experiment.": "{slug} 是当前提供服务的实验。",
    "Nothing is currently serving.": "目前没有实验在提供服务。",
    "Responding to requests:": "是否响应请求：",
    "yes": "是",
    "no": "否",
    "Disk": "磁盘",
    "{used} used of {total} · {free} free": "已用 {used}，共 {total} · 剩余 {free}",
    "PostgreSQL": "PostgreSQL",
    "not reachable": "无法连接",
    "Uploads & backups": "上传与备份",
    "Activity": "活动记录",
    "Nothing has happened yet.": "还没有任何记录。",
    # -- signing in --------------------------------------------------------------
    "Sign in": "登录",
    "Sign out": "退出登录",
    "Password": "密码",
    "This page deploys and manages experiments. Participants never need it — they go straight to the experiment.": "本页面用于部署和管理实验。参与者不需要使用它 — 他们直接进入实验。",
    "Chosen when this server was set up. Once signed in you can change it on the System page.": "密码在服务器安装时设定。登录后可在「系统」页面修改。",
    "No password is set.": "尚未设置密码。",
    "Anyone who can reach this page can stop an experiment or download its data. Set one on the server:": "任何能访问本页面的人都可以停止实验或下载其数据。请在服务器上设置密码：",
    "Access": "访问权限",
    "This page needs a password. The experiment on port {port} does not — participants go straight to it.": "本页面需要密码。端口 {port} 上的实验则不需要 — 参与者可直接访问。",
    "Current password": "当前密码",
    "New password": "新密码",
    "Repeat it": "再次输入",
    "Change password": "修改密码",
    "At least {n} characters. Changing it signs out every other browser.": "至少 {n} 个字符。修改后，其他所有浏览器都会被退出登录。",
    "No password is set, so anyone who can reach this page can stop an experiment or download its data.": "尚未设置密码，因此任何能访问本页面的人都可以停止实验或下载其数据。",
    "Set the first one on the server itself, so nobody on the network can claim it first:": "首次密码需要在服务器上设置，以免网络上的其他人抢先设定：",
    # -- notices from the app ----------------------------------------------------
    "now serving on port {port}": "已在端口 {port} 上提供服务",
    "restarted": "已重启",
    "settings saved; they apply next time it starts": "设置已保存，将在下次启动时生效",
    "no file was uploaded": "没有上传任何文件",
    "the admin password cannot be empty": "管理员密码不能为空",
    "type the project name exactly to confirm the reset": "请准确输入实验名称以确认重置",
    "type the project name exactly to confirm deletion": "请准确输入实验名称以确认删除",
    "that deploy log has expired": "该部署日志已过期",
    "wrong password": "密码不正确",
    "too many attempts; try again in {n} seconds": "尝试次数过多，请在 {n} 秒后重试",
    "the current password is not right": "当前密码不正确",
    "the new passwords did not match": "两次输入的新密码不一致",
    "the password must be at least {n} characters long": "密码至少需要 {n} 个字符",
    "password changed; every other browser has been signed out": "密码已修改，其他所有浏览器均已退出登录",
    "no password is set yet; set the first one on the server": "尚未设置密码，请先在服务器上设置",
    # -- relative times ----------------------------------------------------------
    "never": "从未",
    "just now": "刚刚",
    "{n} min ago": "{n} 分钟前",
    "{n} mins ago": "{n} 分钟前",
    "{n} hour ago": "{n} 小时前",
    "{n} hours ago": "{n} 小时前",
    "{n} day ago": "{n} 天前",
    "{n} days ago": "{n} 天前",
}

CATALOG: dict[str, dict[str, str]] = {"en": {}, "zh": ZH}


def normalize(code: str | None) -> str:
    """Map anything the browser or a URL offers onto a language we actually have."""
    if not code:
        return DEFAULT
    code = code.strip().lower().replace("_", "-")
    if code in LANGUAGES:
        return code
    # zh-cn, zh-hans, zh-hans-cn all mean the same thing here.
    base = code.split("-", 1)[0]
    return base if base in LANGUAGES else DEFAULT


def translate(lang: str, text: str, **params) -> str:
    """Look up ``text``, then fill any placeholders.

    Formatting is applied to the English source too, so a missing translation still
    renders its values. Text that merely happens to contain braces -- an exception
    message, say -- is returned untouched rather than raising.
    """
    if not text:
        return text
    out = CATALOG.get(lang, {}).get(text, text)
    if params:
        try:
            return out.format(**params)
        except (KeyError, IndexError, ValueError):
            return out
    return out


def ago(lang: str, value) -> str:
    """Relative time, translated. Falls back to an absolute date beyond a week."""
    if not value:
        return translate(lang, "never")
    delta = int(time.time()) - int(value)
    if delta < 60:
        return translate(lang, "just now")
    # (divisor, unit, the span it stays valid for). The divisor must match the unit --
    # the version this replaced divided by 3600 and called the result "mins".
    for divisor, unit, limit in ((60, "min", 3600), (3600, "hour", 86400), (86400, "day", 604800)):
        if delta < limit:
            count = delta // divisor
            key = f"{{n}} {unit}" if count == 1 else f"{{n}} {unit}s"
            return translate(lang, f"{key} ago", n=count)
    return time.strftime("%Y-%m-%d", time.localtime(value))
