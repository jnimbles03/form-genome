# app/api/progress.py
from flask import Blueprint, jsonify
import app.services.progress as prog

bp = Blueprint("progress_api", __name__)

@bp.get("/progress")
def get_progress():
    return jsonify(prog.get())