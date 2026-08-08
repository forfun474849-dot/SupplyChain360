from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3
from pathlib import Path
from datetime import date, datetime

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "supplychain360.db"

app = Flask(__name__, template_folder=str(BASE_DIR), static_folder=None)
app.secret_key = "change-this-secret-key-in-production"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contact TEXT,
        email TEXT,
        lead_time INTEGER DEFAULT 7,
        on_time_rate REAL DEFAULT 90,
        quality_rate REAL DEFAULT 95,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        category TEXT,
        supplier_id INTEGER,
        stock INTEGER DEFAULT 0,
        reorder_level INTEGER DEFAULT 10,
        unit_cost REAL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_number TEXT UNIQUE NOT NULL,
        supplier_id INTEGER NOT NULL,
        order_date TEXT NOT NULL,
        expected_date TEXT,
        status TEXT DEFAULT 'Pending',
        total REAL DEFAULT 0,
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    );

    CREATE TABLE IF NOT EXISTS shipments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tracking_no TEXT UNIQUE NOT NULL,
        po_id INTEGER NOT NULL,
        carrier TEXT,
        shipped_date TEXT,
        expected_date TEXT,
        status TEXT DEFAULT 'In Transit',
        FOREIGN KEY (po_id) REFERENCES purchase_orders(id)
    );
    """)
    # Seed data only when the database is empty.
    if conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0:
        conn.executemany(
            """INSERT INTO suppliers
               (name, contact, email, lead_time, on_time_rate, quality_rate)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                ("ABC Components", "Raj", "raj@abc.example", 5, 94, 97),
                ("Global Packaging", "Meena", "meena@global.example", 12, 82, 91),
                ("Prime Logistics", "Arun", "arun@prime.example", 8, 89, 95),
            ],
        )
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        suppliers = conn.execute("SELECT id FROM suppliers ORDER BY id").fetchall()
        conn.executemany(
            """INSERT INTO products
               (sku, name, category, supplier_id, stock, reorder_level, unit_cost)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("SC-1001", "Electronic Controller", "Electronics", suppliers[0]["id"], 42, 20, 1250),
                ("SC-1002", "Packaging Box", "Packaging", suppliers[1]["id"], 8, 15, 35),
                ("SC-1003", "Safety Label", "Consumables", suppliers[2]["id"], 75, 25, 12),
            ],
        )
    conn.commit()
    conn.close()


def supplier_risk(s):
    # Simple transparent score: lower is better.
    risk = 0
    if s["on_time_rate"] < 85:
        risk += 35
    elif s["on_time_rate"] < 92:
        risk += 20
    else:
        risk += 5

    if s["quality_rate"] < 92:
        risk += 35
    elif s["quality_rate"] < 96:
        risk += 15
    else:
        risk += 5

    if s["lead_time"] > 10:
        risk += 25
    elif s["lead_time"] > 7:
        risk += 12
    else:
        risk += 5

    if risk >= 60:
        level = "High"
    elif risk >= 35:
        level = "Medium"
    else:
        level = "Low"
    return risk, level


@app.route("/style.css")
def style_css():
    from flask import send_from_directory
    return send_from_directory(BASE_DIR, "style.css")

@app.context_processor
def inject_globals():
    return {"current_year": datetime.now().year}


@app.route("/")
def dashboard():
    conn = get_db()
    products = conn.execute("""
        SELECT p.*, s.name AS supplier_name
        FROM products p LEFT JOIN suppliers s ON p.supplier_id = s.id
        ORDER BY p.stock ASC
    """).fetchall()
    suppliers = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    orders = conn.execute("""
        SELECT po.*, s.name AS supplier_name
        FROM purchase_orders po JOIN suppliers s ON po.supplier_id=s.id
        ORDER BY po.id DESC LIMIT 8
    """).fetchall()
    shipments = conn.execute("""
        SELECT sh.*, po.po_number
        FROM shipments sh JOIN purchase_orders po ON sh.po_id=po.id
        ORDER BY sh.id DESC LIMIT 6
    """).fetchall()

    stats = {
        "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "suppliers": conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0],
        "low_stock": conn.execute("SELECT COUNT(*) FROM products WHERE stock <= reorder_level").fetchone()[0],
        "orders": conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0],
        "inventory_value": conn.execute("SELECT COALESCE(SUM(stock * unit_cost),0) FROM products").fetchone()[0],
    }
    conn.close()
    return render_template("dashboard.html", products=products, suppliers=suppliers,
                           orders=orders, shipments=shipments, stats=stats)


@app.route("/inventory")
def inventory():
    conn = get_db()
    products = conn.execute("""
        SELECT p.*, s.name AS supplier_name
        FROM products p LEFT JOIN suppliers s ON p.supplier_id=s.id
        ORDER BY p.stock ASC, p.name
    """).fetchall()
    suppliers = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    conn.close()
    return render_template("inventory.html", products=products, suppliers=suppliers)


@app.post("/inventory/add")
def add_product():
    data = request.form
    try:
        conn = get_db()
        conn.execute("""INSERT INTO products
            (sku,name,category,supplier_id,stock,reorder_level,unit_cost)
            VALUES (?,?,?,?,?,?,?)""",
            (data["sku"].strip(), data["name"].strip(), data.get("category",""),
             data.get("supplier_id") or None, int(data.get("stock",0)),
             int(data.get("reorder_level",10)), float(data.get("unit_cost",0))))
        conn.commit()
        conn.close()
        flash("Product added successfully.", "success")
    except sqlite3.IntegrityError:
        flash("SKU already exists. Use a unique SKU.", "danger")
    return redirect(url_for("inventory"))


@app.post("/inventory/<int:product_id>/stock")
def update_stock(product_id):
    try:
        amount = int(request.form["amount"])
        conn = get_db()
        conn.execute("UPDATE products SET stock = MAX(0, stock + ?) WHERE id=?", (amount, product_id))
        conn.commit()
        conn.close()
        flash("Stock updated.", "success")
    except (ValueError, KeyError):
        flash("Enter a valid stock adjustment.", "danger")
    return redirect(url_for("inventory"))


@app.route("/suppliers")
def suppliers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    conn.close()
    enriched = []
    for row in rows:
        risk, level = supplier_risk(row)
        enriched.append({**dict(row), "risk": risk, "risk_level": level})
    return render_template("suppliers.html", suppliers=enriched)


@app.post("/suppliers/add")
def add_supplier():
    d = request.form
    conn = get_db()
    conn.execute("""INSERT INTO suppliers
        (name,contact,email,lead_time,on_time_rate,quality_rate)
        VALUES (?,?,?,?,?,?)""",
        (d["name"].strip(), d.get("contact",""), d.get("email",""),
         int(d.get("lead_time",7)), float(d.get("on_time_rate",90)),
         float(d.get("quality_rate",95))))
    conn.commit()
    conn.close()
    flash("Supplier added successfully.", "success")
    return redirect(url_for("suppliers"))


@app.route("/orders")
def orders():
    conn = get_db()
    rows = conn.execute("""
        SELECT po.*, s.name AS supplier_name
        FROM purchase_orders po JOIN suppliers s ON po.supplier_id=s.id
        ORDER BY po.id DESC
    """).fetchall()
    supplier_rows = conn.execute("SELECT id,name FROM suppliers ORDER BY name").fetchall()
    conn.close()
    return render_template("orders.html", orders=rows, suppliers=supplier_rows)


@app.post("/orders/add")
def add_order():
    d = request.form
    conn = get_db()
    po_number = d["po_number"].strip()
    try:
        conn.execute("""INSERT INTO purchase_orders
            (po_number,supplier_id,order_date,expected_date,status,total)
            VALUES (?,?,?,?,?,?)""",
            (po_number, int(d["supplier_id"]), d.get("order_date") or date.today().isoformat(),
             d.get("expected_date"), d.get("status","Pending"), float(d.get("total",0))))
        conn.commit()
        flash("Purchase order created.", "success")
    except sqlite3.IntegrityError:
        flash("PO number already exists.", "danger")
    finally:
        conn.close()
    return redirect(url_for("orders"))


@app.route("/shipments")
def shipments():
    conn = get_db()
    rows = conn.execute("""
        SELECT sh.*, po.po_number, s.name AS supplier_name
        FROM shipments sh
        JOIN purchase_orders po ON sh.po_id=po.id
        JOIN suppliers s ON po.supplier_id=s.id
        ORDER BY sh.id DESC
    """).fetchall()
    pos = conn.execute("SELECT id,po_number FROM purchase_orders ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("shipments.html", shipments=rows, purchase_orders=pos)


@app.post("/shipments/add")
def add_shipment():
    d = request.form
    conn = get_db()
    try:
        conn.execute("""INSERT INTO shipments
            (tracking_no,po_id,carrier,shipped_date,expected_date,status)
            VALUES (?,?,?,?,?,?)""",
            (d["tracking_no"].strip(), int(d["po_id"]), d.get("shipped_date"),
             d.get("expected_date"), d.get("status","In Transit")))
        conn.commit()
        flash("Shipment added.", "success")
    except sqlite3.IntegrityError:
        flash("Tracking number already exists.", "danger")
    finally:
        conn.close()
    return redirect(url_for("shipments"))


@app.post("/shipments/<int:shipment_id>/status")
def update_shipment(shipment_id):
    status = request.form.get("status", "In Transit")
    conn = get_db()
    conn.execute("UPDATE shipments SET status=? WHERE id=?", (status, shipment_id))
    conn.commit()
    conn.close()
    flash("Shipment status updated.", "success")
    return redirect(url_for("shipments"))


@app.route("/risk")
def risk():
    conn = get_db()
    rows = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    conn.close()
    result = []
    for row in rows:
        score, level = supplier_risk(row)
        result.append({**dict(row), "risk": score, "risk_level": level})
    result.sort(key=lambda x: x["risk"], reverse=True)
    return render_template("risk.html", suppliers=result)


@app.route("/api/dashboard")
def api_dashboard():
    conn = get_db()
    data = {
        "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "suppliers": conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0],
        "low_stock": conn.execute("SELECT COUNT(*) FROM products WHERE stock <= reorder_level").fetchone()[0],
        "purchase_orders": conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0],
        "inventory_value": conn.execute("SELECT COALESCE(SUM(stock*unit_cost),0) FROM products").fetchone()[0],
    }
    conn.close()
    return jsonify(data)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "SupplyChain360"})


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
