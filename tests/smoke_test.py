"""End-to-end smoke test: collect a small access log, then query the dashboard.

Runs with the Python standard library only:

    python3 tests/smoke_test.py
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_line(epoch, user, request, status, micros=120000):
    ts = time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime(epoch))
    return '10.0.0.1 - %s [%s] "%s HTTP/1.1" %d 512 "-" "Mozilla/5.0" %d\n' % (
        user, ts, request, status, micros)


def write_fixtures(tmp):
    now = int(time.time()) - 3600
    lines = [
        log_line(now, "-", "GET /oidc?state=x&code=abc", 302),
        log_line(now + 5, "jdoe01@example.edu", "GET /pun/sys/dashboard/", 200),
        log_line(now + 60, "jdoe01@example.edu",
                 "POST /pun/sys/dashboard/batch_connect/sys/jupyter/session_contexts", 302),
        log_line(now + 90, "jdoe01@example.edu", "GET /pun/sys/files/fs/home/jdoe01", 500, 4500000),
        log_line(now + 120, "asmith02@example.edu", "GET /pun/sys/dashboard/", 200),
    ]
    access_log = os.path.join(tmp, "ood_access_ssl.log")
    with open(access_log, "w") as fh:
        fh.writelines(lines)
    config = os.path.join(tmp, "ood_usage.json")
    with open(config, "w") as fh:
        json.dump({"db_path": os.path.join(tmp, "db", "usage.db"), "db_group": None,
                   "log_globs": [access_log], "walltime": "off"}, fh)
    return config


def call(app, path, query=""):
    status = {}
    def start_response(s, headers):
        status["code"] = s
    body = b"".join(app.application({"PATH_INFO": path, "QUERY_STRING": query,
                                     "SCRIPT_NAME": "", "wsgi.input": io.BytesIO()},
                                    start_response))
    assert status["code"].startswith("200"), "%s returned %s: %s" % (path, status["code"], body[:300])
    return body


def main():
    with tempfile.TemporaryDirectory() as tmp:
        config = write_fixtures(tmp)
        subprocess.run([sys.executable, os.path.join(REPO, "ood-usage-collect"), "-v", "-c", config],
                       check=True)

        os.environ["OOD_USAGE_CONFIG"] = config
        spec = importlib.util.spec_from_file_location("passenger_wsgi", os.path.join(REPO, "passenger_wsgi.py"))
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)

        page = call(app, "/").decode("utf-8")
        assert "__DB_PATH__" not in page and "__CONFIG_PATH__" not in page, "template tokens left unreplaced"
        call(app, "/issues")

        summary = json.loads(call(app, "/api/summary").decode("utf-8"))
        t = summary["totals"]
        assert t["active_users"] == 2, t
        assert t["logins"] == 1, t
        assert t["launches"] == 1, t
        assert [r["app"] for r in summary["launches"]] == ["sys/jupyter"], summary["launches"]
        assert summary["data"]["last_collected"], summary["data"]

        issues = json.loads(call(app, "/api/issues").decode("utf-8"))
        assert issues["totals"]["err5xx"] == 1, issues["totals"]
        assert issues["totals"]["slow"] == 1, issues["totals"]

        assert call(app, "/api/series.csv").startswith(b"period,")
        assert b"jdoe01" in call(app, "/api/users.csv")
        call(app, "/api/summary", "filter=launch:sys/jupyter&group=day")
    print("smoke test passed")


if __name__ == "__main__":
    main()
