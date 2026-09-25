import os
import tempfile
import whisper
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

print("Loading Whisper base model...")
model = whisper.load_model(__import__("os").environ.get("WHISPER_MODEL", "turbo"))
print("Whisper model ready.")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "whisper"})


@app.route("/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    suffix = os.path.splitext(audio_file.filename)[1] if audio_file.filename else ".webm"
    if not suffix:
        suffix = ".webm"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
        audio_file.save(tmp_path)

    try:
        result = model.transcribe(tmp_path)
        text = result.get("text", "").strip()
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5555)
