import http.server
import importlib.util
import json
import threading

from PIL import Image

from servo_embedder_tests.plugin import SKILL

SCRIPT = SKILL / "describe_regions.py"
spec = importlib.util.spec_from_file_location("describe_regions", SCRIPT)
describe_regions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(describe_regions)


def _test_folder(tmp_path):
    (tmp_path / "regions").mkdir()
    Image.new("RGB", (300, 60), "white").save(tmp_path / "regions" / "page-0-0.png")
    Image.new("RGB", (2000, 60), "white").save(tmp_path / "regions" / "page-0-1.png")
    region = {"box": [16, 20, 216, 50], "differing": 900, "elements": [{"text": "Order", "font_weight": "700"}]}
    report = {
        "labels": ["arkweb", "servoshell"],
        "captures": {
            "page-0": {
                "regions": [
                    {**region, "crop": "regions/page-0-0.png"},
                    {**region, "differing": 100, "crop": "regions/page-0-1.png"},
                    {**region, "differing": 50, "crop": None},
                ],
                "dom_changes": [],
            }
        },
    }
    (tmp_path / "diff.json").write_text(json.dumps(report))
    return tmp_path


class _Stub(http.server.BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, self.headers["Authorization"], body))
        content = '```json\n{"regions": [{"region": 1, "description": "title not bold", "verdict": "regression"}]}\n```'
        reply = json.dumps({"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, format, *args):
        pass


def test_one_request_with_the_crops_and_a_cached_answer(tmp_path, monkeypatch, capsys):
    folder = _test_folder(tmp_path)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("SERVO_EMBEDDER_TESTS_VISION_API_KEY", "secret")
    args = [str(folder), "--base-url", f"http://127.0.0.1:{server.server_port}/v1", "--model", "vision-model"]
    try:
        assert describe_regions.main(args) == 0
        assert describe_regions.main(args) == 0
    finally:
        server.shutdown()
    assert len(_Stub.requests) == 1
    path, authorization, body = _Stub.requests[0]
    assert path == "/v1/chat/completions" and authorization == "Bearer secret" and body["model"] == "vision-model"
    parts = body["messages"][0]["content"]
    assert '"arkweb" at the top' in parts[0]["text"] and "EXPECTED" not in parts[0]["text"]
    images = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
    assert len(images) == 2 and all(url.startswith("data:image/png;base64,") for url in images)
    answer = json.loads((folder / "vision.json").read_text())["answer"]
    assert answer["regions"][0]["capture"] == "page-0" and answer["regions"][0]["crop"] == "regions/page-0-0.png"
    assert '"title not bold"' in capsys.readouterr().out


def test_regions_the_measurements_explain_are_not_sent(tmp_path, capsys):
    folder = _test_folder(tmp_path)
    report = json.loads((folder / "diff.json").read_text())
    regions = report["captures"]["page-0"]["regions"]
    regions[0]["reading"] = "moved by (+0, +1.33) CSS px, otherwise nearly the same"
    regions[1]["reading"] = (
        "image /menu/a.jpg did not load in servoshell; 80% less ink: content missing, or much fainter; "
        "shapes differ even at the best alignment: glyphs, typeface or content"
    )
    (folder / "diff.json").write_text(json.dumps(report))
    assert describe_regions.main([str(folder), "--base-url", "http://127.0.0.1:1", "--model", "m"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["regions"] == [] and [r["crop"] for r in answer["explained"]] == [
        "regions/page-0-0.png",
        "regions/page-0-1.png",
    ]
    assert describe_regions.main([str(folder), "--dry-run", "--all-regions"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert sum(part["type"] == "image_url" for part in body["messages"][0]["content"]) == 2


def test_dry_run_sends_nothing(tmp_path, capsys):
    assert describe_regions.main([str(_test_folder(tmp_path)), "--dry-run", "--max-regions", "1"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert [part["type"] for part in body["messages"][0]["content"]] == ["text", "text", "image_url"]
    assert body["messages"][0]["content"][2]["image_url"]["url"].endswith("bytes of PNG>")
