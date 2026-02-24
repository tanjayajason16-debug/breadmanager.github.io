from flask import Flask, render_template, request, jsonify, redirect, send_file
import requests
import json
import csv
import os
from collections import defaultdict
from datetime import datetime

app = Flask(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL_NAME = "arcee-ai/trinity-large-preview:free"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.normpath(os.path.join(BASE_DIR, "..", "data.csv"))
CSV_HEADERS = [
    "ID",
    "Tanggal",
    "Nama",
    "Quantity",
    "HargaPerRoti",
    "Pendapatan",
    "Material",
    "TotalBiaya",
    "Laba",
    "Advice",
]
MONTH_NAMES_ID = {
    1: "Januari",
    2: "Februari",
    3: "Maret",
    4: "April",
    5: "Mei",
    6: "Juni",
    7: "Juli",
    8: "Agustus",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Desember",
}


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


def normalize_row(row):
    date = row.get("Tanggal") or datetime.now().strftime("%Y-%m-%d")
    name = row.get("Nama") or "Roti"

    quantity = max(1, safe_int(row.get("Quantity"), 1))
    revenue = safe_float(row.get("Pendapatan"), 0)
    if "HargaPerRoti" in row and row.get("HargaPerRoti") not in (None, ""):
        price_per_item = safe_float(row.get("HargaPerRoti"), 0)
    else:
        price_per_item = (revenue / quantity) if quantity > 0 else 0

    if "Material" in row and row.get("Material") not in (None, ""):
        material = safe_float(row.get("Material"), 0)
    else:
        # Backward compatibility for old schema: HPP already includes packaging.
        material = safe_float(row.get("HPP"), 0)

    if "TotalBiaya" in row and row.get("TotalBiaya") not in (None, ""):
        total_cost = safe_float(row.get("TotalBiaya"), 0)
    else:
        old_ops = safe_float(row.get("Ops"), 0)
        total_cost = material + old_ops

    if "Laba" in row and row.get("Laba") not in (None, ""):
        profit = safe_float(row.get("Laba"), revenue - total_cost)
    else:
        profit = revenue - total_cost

    entry_id = row.get("ID") or datetime.now().strftime("%Y%m%d%H%M%S")
    advice = row.get("Advice", "")

    return {
        "ID": entry_id,
        "Tanggal": date,
        "Nama": name,
        "Quantity": quantity,
        "HargaPerRoti": price_per_item,
        "Pendapatan": revenue,
        "Material": material,
        "TotalBiaya": total_cost,
        "Laba": profit,
        "Advice": advice,
    }


def read_raw_rows():
    if not os.path.exists(CSV_FILE):
        return [], []

    with open(CSV_FILE, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def write_rows(rows):
    if not rows:
        if os.path.exists(CSV_FILE):
            os.remove(CSV_FILE)
        return

    with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def migrate_if_needed():
    fieldnames, raw_rows = read_raw_rows()
    if not raw_rows:
        return []

    normalized = [normalize_row(row) for row in raw_rows]
    needs_rewrite = fieldnames != CSV_HEADERS

    if needs_rewrite:
        write_rows(normalized)

    return normalized


def get_history():
    _, raw_rows = read_raw_rows()
    history = [normalize_row(row) for row in raw_rows]

    return sorted(
        history,
        key=lambda x: parse_date(x.get("Tanggal")) or datetime.min,
        reverse=True,
    )


def group_history_by_month(history):
    grouped = defaultdict(list)

    for item in history:
        dt = parse_date(item.get("Tanggal"))
        if dt:
            month_key = dt.strftime("%Y-%m")
            month_label = f"{MONTH_NAMES_ID[dt.month]} {dt.year}"
        else:
            month_key = "unknown"
            month_label = "Tanpa Tanggal"

        grouped[(month_key, month_label)].append(item)

    result = []
    for (_, month_label), items in sorted(grouped.items(), key=lambda x: x[0][0], reverse=True):
        result.append({"month_label": month_label, "items": items})

    return result


@app.route("/")
def index():
    history = get_history()
    grouped_history = group_history_by_month(history)
    return render_template("index.html", grouped_history=grouped_history, history_count=len(history))


@app.route("/calculate", methods=["POST"])
def calculate():
    data = request.json or {}
    name = data.get("name", "Roti")
    quantity = max(1, safe_int(data.get("quantity"), 1))
    price_per_item = safe_float(data.get("rev"), 0)
    rev = price_per_item * quantity
    material = safe_float(data.get("material") or data.get("hpp"), 0)
    date = data.get("date") or datetime.now().strftime("%Y-%m-%d")

    # New formula requested: material * 1.6 covers material + operational + packaging.
    total_cost = material * 1.6
    profit = rev - total_cost
    margin = (profit / rev * 100) if rev > 0 else 0

    prompt = (
        f"Analisis bisnis roti {name}. Quantity: {quantity}. Harga per roti: {price_per_item}. Pendapatan total: {rev}. "
        f"Biaya material: {material}. Total biaya (material x 1.6): {total_cost}. "
        f"Laba bersih: {profit}. Berikan 3 saran strategi singkat dalam Bahasa Indonesia."
    )

    if OPENROUTER_API_KEY:
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "model": MODEL_NAME,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                ),
                timeout=20,
            )
            response.raise_for_status()
            advice = response.json()["choices"][0]["message"]["content"]
        except Exception:
            advice = "Saran AI tidak tersedia saat ini. Periksa koneksi internet atau API Key."
    else:
        advice = "OPENROUTER_API_KEY belum di-set. Jalankan app dengan API key di environment variable."

    entry_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    entry = {
        "ID": entry_id,
        "Tanggal": date,
        "Nama": name,
        "Quantity": quantity,
        "HargaPerRoti": price_per_item,
        "Pendapatan": rev,
        "Material": material,
        "TotalBiaya": total_cost,
        "Laba": profit,
        "Advice": advice,
    }

    existing = migrate_if_needed()
    existing.append(entry)
    write_rows(existing)

    return jsonify(
        {
            "profit": profit,
            "margin": margin,
            "advice": advice,
            "name": name,
            "date": date,
            "quantity": quantity,
            "price_per_item": price_per_item,
            "revenue": rev,
            "total_cost": total_cost,
        }
    )


@app.route("/export")
def export_data():
    if os.path.exists(CSV_FILE):
        return send_file(
            CSV_FILE,
            as_attachment=True,
            download_name=f"bakery_data_{datetime.now().strftime('%Y%m%d')}.csv",
        )
    return "File data.csv tidak ditemukan!", 404


@app.route("/import", methods=["POST"])
def import_data():
    if "file" not in request.files:
        return redirect("/")

    file = request.files["file"]
    if file and file.filename.endswith(".csv"):
        file.save(CSV_FILE)
        migrate_if_needed()

    return redirect("/")


@app.route("/delete/<entry_id>")
def delete_one(entry_id):
    history = get_history()
    new_history = [row for row in history if row["ID"] != entry_id]
    write_rows(new_history)
    return redirect("/")


@app.route("/clear")
def clear_all():
    if os.path.exists(CSV_FILE):
        os.remove(CSV_FILE)
    return redirect("/")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)




