"""실행: python run.py  →  브라우저에서 http://127.0.0.1:5000

옵션
  --host 0.0.0.0   같은 와이파이의 휴대폰·다른 PC에서도 접속
  --port 5000      포트 변경
  --scan           웹 서버 없이 스캔 1회만 실행 (OS 작업 스케줄러용)
"""
import argparse
import webbrowser

from stockapp import db

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--demo", action="store_true", help="--scan과 함께 쓰면 데모 데이터로 스캔")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.scan:
        from stockapp import scanner
        db.init()
        sid = scanner.run_scan("cli", a.demo)
        s = db.scan(sid)
        print(f"스캔 #{sid}: {s['status']} · {s['message']}")
    else:
        from stockapp.web import create_app
        app = create_app()
        url = f"http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{a.port}"
        print(f"\n  대시보드: {url}  (종료: Ctrl+C)\n")
        if not a.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        app.run(host=a.host, port=a.port, debug=False, use_reloader=False, threaded=True)
