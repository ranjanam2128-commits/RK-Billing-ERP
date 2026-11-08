"""
RK BILLING ERP - Desktop Billing & Inventory
--------------------------------------------
Requirements: Python 3.10 or newer. No pip packages required.
Run: python rk_billing_erp.py
The SQLite database (rk_billing.db) is created automatically beside this file.
"""

import os
import csv
import sqlite3
import shutil
import subprocess
import sys
import hashlib
import secrets
import uuid
import re
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import date, datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "rk_billing.db")


def as_float(value, field="value"):
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except ValueError:
        raise ValueError(f"{field} में सही संख्या डालें।")


def fmt(value):
    return f"{float(value or 0):,.2f}"


class Store:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'Pcs',
            purchase_rate REAL NOT NULL DEFAULT 0,
            sales_rate REAL NOT NULL DEFAULT 0,
            stock REAL NOT NULL DEFAULT 0,
            min_stock REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS parties(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            mobile TEXT DEFAULT '',
            address TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,
            inv_date TEXT NOT NULL,
            party TEXT NOT NULL,
            mobile TEXT DEFAULT '',
            person TEXT DEFAULT '',
            subtotal REAL NOT NULL DEFAULT 0,
            discount REAL NOT NULL DEFAULT 0,
            tax REAL NOT NULL DEFAULT 0,
            grand REAL NOT NULL DEFAULT 0,
            paid REAL NOT NULL DEFAULT 0,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS invoice_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            batch TEXT DEFAULT '',
            unit TEXT DEFAULT 'Pcs',
            qty REAL NOT NULL,
            rate REAL NOT NULL,
            amount REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            code TEXT NOT NULL,
            movement TEXT NOT NULL,
            qty REAL NOT NULL,
            reference TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS store_profile(
            id INTEGER PRIMARY KEY CHECK(id=1),
            shop_name TEXT DEFAULT '',
            owner_name TEXT DEFAULT '',
            mobile TEXT DEFAULT '',
            address TEXT DEFAULT '',
            gstin TEXT DEFAULT '',
            email TEXT DEFAULT ''
        );
        INSERT OR IGNORE INTO store_profile(id) VALUES(1);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def get_store_profile(self):
        return self.con.execute("SELECT * FROM store_profile WHERE id=1").fetchone()

    def save_store_profile(self, shop_name, owner_name, mobile, address, gstin, email):
        self.con.execute("""UPDATE store_profile SET shop_name=?, owner_name=?, mobile=?,
            address=?, gstin=?, email=? WHERE id=1""",
            (shop_name, owner_name, mobile, address, gstin, email))
        self.con.commit()

    def products(self):
        return self.con.execute("SELECT * FROM products ORDER BY name").fetchall()

    def get_product(self, code):
        return self.con.execute("SELECT * FROM products WHERE code=?", (code,)).fetchone()

    def save_product(self, code, name, unit, purchase, sales, stock, minimum, old_code=None):
        if old_code:
            self.con.execute("""UPDATE products SET code=?,name=?,unit=?,purchase_rate=?,
                sales_rate=?,stock=?,min_stock=? WHERE code=?""",
                (code, name, unit, purchase, sales, stock, minimum, old_code))
        else:
            self.con.execute("""INSERT INTO products
                (code,name,unit,purchase_rate,sales_rate,stock,min_stock)
                VALUES(?,?,?,?,?,?,?)""",
                (code, name, unit, purchase, sales, stock, minimum))
        self.con.commit()

    def delete_product(self, code):
        used = self.con.execute("SELECT COUNT(*) FROM invoice_items WHERE code=?", (code,)).fetchone()[0]
        if used:
            raise ValueError("यह product पुराने bill में इस्तेमाल हुआ है, इसलिए delete नहीं कर सकते।")
        self.con.execute("DELETE FROM products WHERE code=?", (code,))
        self.con.commit()

    def party_names(self):
        return [r["name"] for r in self.con.execute("SELECT name FROM parties ORDER BY name")]

    def save_party(self, name, mobile="", address=""):
        self.con.execute("INSERT OR IGNORE INTO parties(name,mobile,address) VALUES(?,?,?)",
                         (name, mobile, address))
        self.con.commit()

    def next_voucher(self, kind):
        prefix = "S" if kind == "SALE" else "P"
        row = self.con.execute(
            "SELECT voucher FROM invoices WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)
        ).fetchone()
        number = 1001
        if row:
            try:
                number = int(row["voucher"][1:]) + 1
            except (ValueError, IndexError):
                number = 1001
        return f"{prefix}{number}"

    def create_invoice(self, data, items):
        cur = self.con.cursor()
        try:
            cur.execute("""INSERT INTO invoices
                (voucher,kind,inv_date,party,mobile,person,subtotal,discount,tax,grand,paid,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["voucher"], data["kind"], data["date"], data["party"], data["mobile"],
                 data["person"], data["subtotal"], data["discount"], data["tax"],
                 data["grand"], data["paid"], data["notes"]))
            invoice_id = cur.lastrowid
            for it in items:
                cur.execute("""INSERT INTO invoice_items
                    (invoice_id,code,name,batch,unit,qty,rate,amount)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (invoice_id, it["code"], it["name"], it["batch"], it["unit"],
                     it["qty"], it["rate"], it["amount"]))
                # Sale reduces stock; purchase adds stock.
                delta = it["qty"] if data["kind"] == "PURCHASE" else -it["qty"]
                cur.execute("UPDATE products SET stock=stock+? WHERE code=?", (delta, it["code"]))
                cur.execute("""INSERT INTO stock_log(log_date,code,movement,qty,reference)
                    VALUES(?,?,?,?,?)""",
                    (data["date"], it["code"], data["kind"], it["qty"], data["voucher"]))
            self.con.commit()
            return invoice_id
        except Exception:
            self.con.rollback()
            raise

    def invoices(self, search=""):
        q = f"%{search}%"
        return self.con.execute("""SELECT * FROM invoices
            WHERE voucher LIKE ? OR party LIKE ? OR mobile LIKE ?
            ORDER BY id DESC""", (q, q, q)).fetchall()

    def invoice_detail(self, invoice_id):
        inv = self.con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        items = self.con.execute(
            "SELECT * FROM invoice_items WHERE invoice_id=?", (invoice_id,)
        ).fetchall()
        return inv, items

    def delete_invoice(self, invoice_id):
        cur = self.con.cursor()
        try:
            inv = cur.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if not inv:
                return
            items = cur.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice_id,)).fetchall()
            for it in items:
                delta = -it["qty"] if inv["kind"] == "PURCHASE" else it["qty"]
                cur.execute("UPDATE products SET stock=stock+? WHERE code=?", (delta, it["code"]))
                cur.execute("""INSERT INTO stock_log(log_date,code,movement,qty,reference)
                    VALUES(?,?,?,?,?)""",
                    (date.today().isoformat(), it["code"], "DELETE-" + inv["kind"],
                     it["qty"], inv["voucher"]))
            cur.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def summary(self):
        c = self.con
        return {
            "products": c.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "stock": c.execute("SELECT COALESCE(SUM(stock),0) FROM products").fetchone()[0],
            "sales": c.execute("SELECT COALESCE(SUM(grand),0) FROM invoices WHERE kind='SALE'").fetchone()[0],
            "purchase": c.execute("SELECT COALESCE(SUM(grand),0) FROM invoices WHERE kind='PURCHASE'").fetchone()[0],
            "due": c.execute("SELECT COALESCE(SUM(grand-paid),0) FROM invoices").fetchone()[0],
        }


class ERP(tk.Tk):
    NAVY = "#062653"
    BLUE = "#0757ae"
    BG = "#edf3fa"
    GREEN = "#079447"
    RED = "#c82333"

    def __init__(self, db_path=None, username='admin', role='admin', shop_name=''):
        super().__init__()
        self.username = username
        self.user_role = role
        self.shop_name = shop_name
        self.title('RK Billing ERP - Sales, Purchase & Inventory')
        # Fit the application to the actual laptop display instead of forcing
        # a 1450x850 window that may extend beyond smaller screens.
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(1450, max(850, screen_w - 30))
        win_h = min(850, max(550, screen_h - 80))
        self.geometry(f"{win_w}x{win_h}+10+10")
        self.minsize(800, 520)
        self.configure(bg=self.BG)
        self.bind("<F11>", self.toggle_fullscreen)
        self.bind("<Escape>", self.exit_fullscreen)
        # Maximize on Windows; if unsupported, keep the screen-fitted geometry.
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.db = Store(db_path)
        self.cart = []
        self.current_invoice_id = None
        self.build_style()
        self.build_layout()
        self.show_dashboard()
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", rowheight=27, font=("Segoe UI", 10),
                     background="white", fieldbackground="white")
        st.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"),
                     background="#d7e8fb", foreground="#102d50")
        st.configure("TEntry", padding=5)
        st.configure("TCombobox", padding=5)

    def build_layout(self):
        header = tk.Frame(self, bg=self.NAVY, height=62)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="RK", bg=self.NAVY, fg="white",
                 font=("Segoe UI", 24, "bold")).pack(side="left", padx=(18, 4))
        tk.Label(header, text="BILLING ERP", bg=self.NAVY, fg="#ffdc54",
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(header, text="   Sales  |  Purchase  |  Inventory  |  Reports",
                 bg=self.NAVY, fg="white", font=("Segoe UI", 11)).pack(side="left", padx=20)
        tk.Button(header, text="Logout", command=self.logout,
                  bg="#a8323e", fg="white", relief="flat", padx=12).pack(side="right", padx=8, pady=13)
        tk.Label(header, text=f"{self.shop_name or self.username} | {self.username}",
                 bg=self.NAVY, fg="white", font=("Segoe UI", 9, "bold")).pack(side="right", padx=8)
        tk.Button(header, text="Backup Database", command=self.backup,
                  bg="#1767b7", fg="white", relief="flat", padx=12).pack(side="right", padx=8, pady=13)

        body = tk.Frame(self, bg=self.BG)
        body.pack(fill="both", expand=True)
        side = tk.Frame(body, bg="#08264f", width=210)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        # Scrollable work area: lower invoice controls remain reachable on short screens.
        work_area = tk.Frame(body, bg=self.BG)
        work_area.pack(side="left", fill="both", expand=True)
        self.work_canvas = tk.Canvas(work_area, bg=self.BG, highlightthickness=0)
        self.work_scrollbar = ttk.Scrollbar(work_area, orient="vertical", command=self.work_canvas.yview)
        self.work_canvas.configure(yscrollcommand=self.work_scrollbar.set)
        self.work_scrollbar.pack(side="right", fill="y")
        self.work_canvas.pack(side="left", fill="both", expand=True)
        self.main = tk.Frame(self.work_canvas, bg=self.BG)
        self.main_window = self.work_canvas.create_window((0, 0), window=self.main, anchor="nw")
        self.main.bind("<Configure>", self._update_scroll_region)
        self.work_canvas.bind("<Configure>", self._resize_work_area)
        self.work_canvas.bind_all("<MouseWheel>", self._mousewheel)

        nav = [
            ("⌂   Dashboard", self.show_dashboard),
            ("🛒  Sales Invoice", lambda: self.show_invoice("SALE")),
            ("📥  Purchase Invoice", lambda: self.show_invoice("PURCHASE")),
            ("▤   Product / Stock", self.show_products),
            ("▣   Invoice Records", self.show_records),
            ("▤   Stock Report", self.show_stock_report),
            ("🏪  Shop Settings", self.show_shop_settings),
            ("⬇   Export Records", self.export_records),
            ("⚙   Backup", self.backup),
        ]
        tk.Label(side, text="RK BILLING", bg="#08264f", fg="#ffdc54",
                 font=("Segoe UI", 16, "bold")).pack(pady=(25, 20))
        if getattr(self, "user_role", "shop") == "admin":
            nav.append(("👥  Manage Shop Accounts", self.manage_accounts))
        for label, command in nav:
            tk.Button(side, text=label, command=command, anchor="w",
                      bg="#103967", fg="white", activebackground="#1767b7",
                      activeforeground="white", relief="flat", font=("Segoe UI", 10, "bold"),
                      padx=15, pady=13).pack(fill="x", padx=8, pady=3)
        tk.Label(side, text="Local database\nrk_billing.db", bg="#08264f",
                 fg="#c9d8ec", font=("Segoe UI", 9)).pack(side="bottom", pady=18)

    def _update_scroll_region(self, _event=None):
        self.work_canvas.configure(scrollregion=self.work_canvas.bbox("all"))

    def _resize_work_area(self, event):
        self.work_canvas.itemconfigure(self.main_window, width=event.width)

    def _mousewheel(self, event):
        # Scroll the central work area with the mouse wheel.
        if getattr(self, "work_canvas", None) and self.work_canvas.winfo_exists():
            self.work_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear_main(self):
        for child in self.main.winfo_children():
            child.destroy()

    def page_title(self, title, subtitle=""):
        top = tk.Frame(self.main, bg=self.BG)
        top.pack(fill="x", padx=20, pady=(17, 10))
        tk.Label(top, text=title, bg=self.BG, fg=self.NAVY,
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(top, text=subtitle, bg=self.BG, fg="#53677e",
                     font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))

    def button(self, parent, text, command, color=None):
        return tk.Button(parent, text=text, command=command, bg=color or self.BLUE,
                         fg="white", activeforeground="white", relief="flat",
                         padx=14, pady=8, font=("Segoe UI", 10, "bold"), cursor="hand2")

    def show_dashboard(self):
        self.clear_main()
        self.page_title("Dashboard", "आपके business का वर्तमान summary")
        s = self.db.summary()
        cards = [
            ("Total Products", s["products"], "#1767b7"),
            ("Total Stock Qty", fmt(s["stock"]), "#16835a"),
            ("Total Sales", "₹ " + fmt(s["sales"]), "#6d45ad"),
            ("Total Purchase", "₹ " + fmt(s["purchase"]), "#bd7413"),
            ("Outstanding Due", "₹ " + fmt(s["due"]), "#bd3344"),
        ]
        row = tk.Frame(self.main, bg=self.BG)
        row.pack(fill="x", padx=14, pady=12)
        for title, value, color in cards:
            card = tk.Frame(row, bg="white", highlightbackground="#d4dfeb", highlightthickness=1)
            card.pack(side="left", fill="both", expand=True, padx=6, ipady=18)
            tk.Label(card, text=title, bg="white", fg="#53677e",
                     font=("Segoe UI", 10, "bold")).pack(pady=(12, 5))
            tk.Label(card, text=str(value), bg="white", fg=color,
                     font=("Segoe UI", 17, "bold")).pack(pady=(0, 12))
        actions = tk.Frame(self.main, bg=self.BG)
        actions.pack(anchor="w", padx=25, pady=20)
        self.button(actions, "+ New Sale", lambda: self.show_invoice("SALE"), self.GREEN).pack(side="left", padx=6)
        self.button(actions, "+ New Purchase", lambda: self.show_invoice("PURCHASE"), "#bd7413").pack(side="left", padx=6)
        self.button(actions, "View Old Records", self.show_records).pack(side="left", padx=6)
        self.button(actions, "Manage Products", self.show_products).pack(side="left", padx=6)
        note = tk.Label(self.main,
            text="ध्यान दें: यह software data इसी folder की rk_billing.db file में save करता है। नियमित Backup Database करें।",
            bg="#fff7df", fg="#725315", font=("Segoe UI", 10), padx=15, pady=12, anchor="w")
        note.pack(fill="x", padx=25, pady=15)

    def show_invoice(self, kind):
        self.cart = []
        self.clear_main()
        label = "Sales Invoice" if kind == "SALE" else "Purchase Invoice"
        self.page_title(label, "Product चुनें, quantity और rate भरें, फिर invoice save करें।")
        self.inv_kind = kind

        form = tk.Frame(self.main, bg="white", padx=12, pady=10)
        form.pack(fill="x", padx=18, pady=5)
        self.voucher_var = tk.StringVar(value=self.db.next_voucher(kind))
        self.date_var = tk.StringVar(value=date.today().strftime("%d-%b-%Y"))
        self.party_var = tk.StringVar(value="Cash Account")
        self.mobile_var = tk.StringVar()
        self.person_var = tk.StringVar()
        self.notes_var = tk.StringVar()
        self.field(form, "Voucher No.", self.voucher_var, 0, 0, width=17)
        self.field(form, "Date (DD-MMM-YYYY)", self.date_var, 0, 2, width=18)
        self.field(form, "Party / Customer / Supplier", self.party_var, 1, 0, width=28)
        self.field(form, "Mobile No.", self.mobile_var, 1, 2, width=18)
        self.field(form, "Sales Person", self.person_var, 2, 0, width=28)
        self.field(form, "Notes", self.notes_var, 2, 2, width=28)

        addbox = tk.LabelFrame(self.main, text="Add Product", bg=self.BG,
                               fg=self.NAVY, font=("Segoe UI", 10, "bold"), padx=10, pady=8)
        addbox.pack(fill="x", padx=18, pady=8)
        self.prod_var = tk.StringVar()
        product_values = [f'{p["code"]} | {p["name"]}' for p in self.db.products()]
        ttk.Label(addbox, text="Product").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.prod_combo = ttk.Combobox(addbox, textvariable=self.prod_var,
                                       values=product_values, width=35, state="readonly")
        self.prod_combo.grid(row=0, column=1, padx=5, pady=5)
        self.prod_combo.bind("<<ComboboxSelected>>", self.product_selected)
        self.batch_var = tk.StringVar()
        self.qty_var = tk.StringVar(value="1")
        self.rate_var = tk.StringVar(value="0")
        self.unit_var = tk.StringVar(value="Pcs")
        for col, title, var, width in [
            (2, "Batch", self.batch_var, 10), (4, "Qty", self.qty_var, 8),
            (6, "Rate", self.rate_var, 10), (8, "Unit", self.unit_var, 8)
        ]:
            ttk.Label(addbox, text=title).grid(row=0, column=col, padx=4)
            ttk.Entry(addbox, textvariable=var, width=width).grid(row=0, column=col+1, padx=4)
        self.button(addbox, "Add Item", self.add_item, self.GREEN).grid(row=0, column=10, padx=8)

        tablebox = tk.Frame(self.main, bg=self.BG)
        tablebox.pack(fill="x", padx=18, pady=4)
        cols = ("code", "product", "batch", "unit", "qty", "rate", "amount")
        self.cart_tree = ttk.Treeview(tablebox, columns=cols, show="headings", height=6)
        headings = {"code":"Code", "product":"Product", "batch":"Batch", "unit":"Unit",
                    "qty":"Qty", "rate":"Rate", "amount":"Amount"}
        widths = {"code":100, "product":300, "batch":100, "unit":70, "qty":80, "rate":110, "amount":130}
        for c in cols:
            self.cart_tree.heading(c, text=headings[c])
            self.cart_tree.column(c, width=widths[c], anchor="center")
        self.cart_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(tablebox, orient="vertical", command=self.cart_tree.yview)
        self.cart_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        bottom = tk.Frame(self.main, bg="white", padx=15, pady=10)
        bottom.pack(fill="x", padx=18, pady=(5, 15))
        self.total_var = tk.StringVar(value="0.00")
        self.discount_var = tk.StringVar(value="0")
        self.tax_var = tk.StringVar(value="0")
        self.paid_var = tk.StringVar(value="0")
        self.grand_var = tk.StringVar(value="0.00")
        self.balance_var = tk.StringVar(value="0.00")
        tk.Label(bottom, text="Discount ₹", bg="white").grid(row=0, column=0, padx=4)
        ttk.Entry(bottom, textvariable=self.discount_var, width=11).grid(row=0, column=1, padx=4)
        tk.Label(bottom, text="Tax ₹", bg="white").grid(row=0, column=2, padx=4)
        ttk.Entry(bottom, textvariable=self.tax_var, width=11).grid(row=0, column=3, padx=4)
        tk.Label(bottom, text="Paid ₹", bg="white").grid(row=0, column=4, padx=4)
        ttk.Entry(bottom, textvariable=self.paid_var, width=11).grid(row=0, column=5, padx=4)
        tk.Label(bottom, text="Subtotal:", bg="white", font=("Segoe UI", 10, "bold")).grid(row=1, column=0, pady=9)
        tk.Label(bottom, textvariable=self.total_var, bg="white", fg=self.NAVY,
                 font=("Segoe UI", 11, "bold")).grid(row=1, column=1, sticky="w")
        tk.Label(bottom, text="Grand Total:", bg="white", font=("Segoe UI", 12, "bold")).grid(row=1, column=2)
        tk.Label(bottom, textvariable=self.grand_var, bg="white", fg=self.RED,
                 font=("Segoe UI", 16, "bold")).grid(row=1, column=3, sticky="w")
        tk.Label(bottom, text="Balance:", bg="white", font=("Segoe UI", 10, "bold")).grid(row=1, column=4)
        tk.Label(bottom, textvariable=self.balance_var, bg="white",
                 font=("Segoe UI", 11, "bold")).grid(row=1, column=5, sticky="w")
        self.discount_var.trace_add("write", lambda *_: self.recalculate())
        self.tax_var.trace_add("write", lambda *_: self.recalculate())
        self.paid_var.trace_add("write", lambda *_: self.recalculate())
        self.button(bottom, "Remove Selected", self.remove_cart_item, self.RED).grid(row=0, column=6, padx=5)
        self.button(bottom, "Save Invoice", self.save_invoice, self.GREEN).grid(row=1, column=6, padx=5, pady=5)
        self.button(bottom, "Clear", lambda: self.show_invoice(kind), "#63758a").grid(row=0, column=7, padx=5)

    def field(self, parent, label, var, row, col, width=20):
        tk.Label(parent, text=label, bg="white", fg="#263c56",
                 font=("Segoe UI", 9, "bold")).grid(row=row, column=col, sticky="w", padx=6, pady=5)
        ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=col+1, sticky="w", padx=6, pady=5)

    def product_selected(self, _event=None):
        selected = self.prod_var.get()
        code = selected.split("|", 1)[0].strip()
        p = self.db.get_product(code)
        if p:
            self.unit_var.set(p["unit"])
            rate = p["sales_rate"] if self.inv_kind == "SALE" else p["purchase_rate"]
            self.rate_var.set(str(rate))

    def add_item(self):
        try:
            selected = self.prod_var.get()
            if not selected:
                raise ValueError("पहले product चुनें।")
            code = selected.split("|", 1)[0].strip()
            p = self.db.get_product(code)
            if not p:
                raise ValueError("Product नहीं मिला।")
            qty = as_float(self.qty_var.get(), "Qty")
            rate = as_float(self.rate_var.get(), "Rate")
            if qty <= 0 or rate < 0:
                raise ValueError("Qty शून्य से अधिक और rate शून्य या अधिक होना चाहिए।")
            if self.inv_kind == "SALE" and qty > p["stock"]:
                raise ValueError(f'इतना stock उपलब्ध नहीं है। Current stock: {fmt(p["stock"])}')
            item = {"code": code, "name": p["name"], "batch": self.batch_var.get().strip(),
                    "unit": self.unit_var.get().strip() or p["unit"], "qty": qty,
                    "rate": rate, "amount": qty * rate}
            self.cart.append(item)
            self.refresh_cart()
            self.prod_var.set("")
            self.qty_var.set("1")
            self.rate_var.set("0")
            self.batch_var.set("")
        except ValueError as e:
            messagebox.showerror("Product", str(e))

    def refresh_cart(self):
        for row in self.cart_tree.get_children():
            self.cart_tree.delete(row)
        for i in self.cart:
            self.cart_tree.insert("", "end", values=(
                i["code"], i["name"], i["batch"], i["unit"], fmt(i["qty"]),
                fmt(i["rate"]), fmt(i["amount"])))
        self.recalculate()

    def remove_cart_item(self):
        selected = self.cart_tree.selection()
        if not selected:
            messagebox.showwarning("Select", "हटाने के लिए item चुनें।")
            return
        index = self.cart_tree.index(selected[0])
        self.cart.pop(index)
        self.refresh_cart()

    def recalculate(self):
        if not hasattr(self, "total_var"):
            return
        subtotal = sum(x["amount"] for x in self.cart)
        try:
            discount = as_float(self.discount_var.get(), "Discount")
            tax = as_float(self.tax_var.get(), "Tax")
            paid = as_float(self.paid_var.get(), "Paid")
        except ValueError:
            discount = tax = paid = 0
        grand = max(0, subtotal - discount + tax)
        self.total_var.set(fmt(subtotal))
        self.grand_var.set(fmt(grand))
        self.balance_var.set(fmt(grand - paid))

    def save_invoice(self):
        try:
            if not self.cart:
                raise ValueError("Invoice में कम से कम एक product जोड़ें।")
            voucher = self.voucher_var.get().strip()
            party = self.party_var.get().strip()
            if not voucher or not party:
                raise ValueError("Voucher No. और Party जरूरी हैं।")
            # Validate date and numeric fields before writing.
            try:
                inv_date = datetime.strptime(self.date_var.get().strip(), "%d-%b-%Y").date().isoformat()
            except ValueError:
                try:
                    inv_date = datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d").date().isoformat()
                except ValueError:
                    raise ValueError("Date DD-MMM-YYYY format में लिखें, जैसे 07-Nov-2026।")
            subtotal = sum(x["amount"] for x in self.cart)
            discount = as_float(self.discount_var.get(), "Discount")
            tax = as_float(self.tax_var.get(), "Tax")
            paid = as_float(self.paid_var.get(), "Paid")
            if min(discount, tax, paid) < 0:
                raise ValueError("Discount, Tax और Paid negative नहीं हो सकते।")
            grand = max(0, subtotal - discount + tax)
            if paid > grand:
                raise ValueError("Paid amount Grand Total से अधिक नहीं हो सकता।")
            data = {"voucher": voucher, "kind": self.inv_kind, "date": inv_date,
                    "party": party, "mobile": self.mobile_var.get().strip(),
                    "person": self.person_var.get().strip(), "subtotal": subtotal,
                    "discount": discount, "tax": tax, "grand": grand, "paid": paid,
                    "notes": self.notes_var.get().strip()}
            self.db.save_party(party, data["mobile"])
            invoice_id = self.db.create_invoice(data, self.cart)
            messagebox.showinfo("Saved", f"Invoice {voucher} सफलतापूर्वक save हो गया।")
            self.show_invoice(self.inv_kind)
            self.show_invoice_preview(invoice_id)
        except (ValueError, sqlite3.IntegrityError) as e:
            messagebox.showerror("Save Invoice", str(e))

    def show_invoice_preview(self, invoice_id):
        inv, items = self.db.invoice_detail(invoice_id)
        if not inv:
            return
        win = tk.Toplevel(self)
        win.title("Invoice - " + inv["voucher"])
        win.geometry("760x650")
        text = tk.Text(win, font=("Consolas", 10), wrap="none")
        text.pack(fill="both", expand=True, padx=10, pady=10)
        shop = self.db.get_store_profile()
        shop_name = (shop["shop_name"] or "RK BILLING ERP").center(90)
        lines = [
            shop_name,
            (shop["address"] or "").center(90),
            (("Mobile: " + shop["mobile"]) if shop["mobile"] else "").center(90),
            (("GSTIN: " + shop["gstin"]) if shop["gstin"] else "").center(90),
            "                         TAX / SALES INVOICE",
            "-" * 90,
            f'Voucher: {inv["voucher"]:<20} Date: {inv["inv_date"]}',
            f'Party: {inv["party"]:<25} Mobile: {inv["mobile"]}',
            "-" * 90,
            f'{"Code":<12} {"Product":<25} {"Qty":>8} {"Rate":>12} {"Amount":>14}',
            "-" * 90,
        ]
        for it in items:
            lines.append(f'{it["code"]:<12} {it["name"][:25]:<25} {it["qty"]:>8g} {fmt(it["rate"]):>12} {fmt(it["amount"]):>14}')
        lines += [
            "-" * 90, f'Subtotal: {fmt(inv["subtotal"]):>75}',
            f'Discount: {fmt(inv["discount"]):>75}', f'Tax: {fmt(inv["tax"]):>80}',
            f'GRAND TOTAL: {fmt(inv["grand"]):>70}', f'Paid: {fmt(inv["paid"]):>78}',
            f'Balance: {fmt(inv["grand"]-inv["paid"]):>75}', "-" * 90,
            f'Notes: {inv["notes"]}', "                    Thank you for your business!"
        ]
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        bar = tk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        self.button(bar, "Print", lambda: self.print_invoice(inv, items)).pack(side="left")

    def print_invoice(self, inv, items):
        path = os.path.join(APP_DIR, f'{inv["voucher"]}_invoice.txt')
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'RK BILLING ERP\\nVoucher: {inv["voucher"]}\\nDate: {inv["inv_date"]}\\nParty: {inv["party"]}\\n\\n')
            f.write("Code | Product | Qty | Rate | Amount\\n")
            for it in items:
                f.write(f'{it["code"]} | {it["name"]} | {it["qty"]} | {fmt(it["rate"])} | {fmt(it["amount"])}\\n')
            f.write(f'\\nSubtotal: {fmt(inv["subtotal"])}\\nDiscount: {fmt(inv["discount"])}\\nTax: {fmt(inv["tax"])}\\nGrand Total: {fmt(inv["grand"])}\\nPaid: {fmt(inv["paid"])}\\nBalance: {fmt(inv["grand"]-inv["paid"])}\\n')
        try:
            if sys.platform.startswith("win"):
                os.startfile(path, "print")
            else:
                subprocess.Popen(["lp", path])
        except Exception:
            os.startfile(path) if sys.platform.startswith("win") else messagebox.showinfo("Saved", f"Print के लिए file बनाई गई: {path}")

    def show_products(self):
        self.clear_main()
        self.page_title("Product Master / Stock", "अपने product, code, unit, rate और opening stock जोड़ें या बदलें।")
        form = tk.LabelFrame(self.main, text="Product Details", bg="white", padx=10, pady=10)
        form.pack(fill="x", padx=18, pady=8)
        self.p_code = tk.StringVar()
        self.p_name = tk.StringVar()
        self.p_unit = tk.StringVar(value="Pcs")
        self.p_purchase = tk.StringVar(value="0")
        self.p_sales = tk.StringVar(value="0")
        self.p_stock = tk.StringVar(value="0")
        self.p_min = tk.StringVar(value="0")
        self.old_product_code = None
        fields = [("Code", self.p_code), ("Product Name", self.p_name), ("Unit", self.p_unit),
                  ("Purchase Rate", self.p_purchase), ("Sales Rate", self.p_sales),
                  ("Opening / Current Stock", self.p_stock), ("Minimum Stock", self.p_min)]
        for idx, (label, var) in enumerate(fields):
            r, c = divmod(idx, 4)
            tk.Label(form, text=label, bg="white").grid(row=r*2, column=c, sticky="w", padx=5, pady=(3, 0))
            ttk.Entry(form, textvariable=var, width=22).grid(row=r*2+1, column=c, padx=5, pady=3, sticky="w")
        actions = tk.Frame(form, bg="white")
        actions.grid(row=4, column=0, columnspan=4, sticky="w", pady=7)
        self.button(actions, "Save Product", self.save_product, self.GREEN).pack(side="left", padx=4)
        self.button(actions, "Clear Form", self.clear_product_form, "#63758a").pack(side="left", padx=4)
        self.button(actions, "Delete Selected", self.delete_product, self.RED).pack(side="left", padx=4)

        box = tk.Frame(self.main, bg=self.BG)
        box.pack(fill="both", expand=True, padx=18, pady=8)
        cols = ("code", "name", "unit", "purchase_rate", "sales_rate", "stock", "min_stock")
        self.product_tree = ttk.Treeview(box, columns=cols, show="headings")
        titles = ["Code", "Product Name", "Unit", "Purchase Rate", "Sales Rate", "Stock", "Min Stock"]
        for c, title in zip(cols, titles):
            self.product_tree.heading(c, text=title)
            self.product_tree.column(c, width=130, anchor="center")
        self.product_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.product_tree.yview)
        self.product_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.product_tree.bind("<<TreeviewSelect>>", self.load_product)
        self.refresh_products()

    def refresh_products(self):
        if not hasattr(self, "product_tree"):
            return
        self.product_tree.delete(*self.product_tree.get_children())
        for p in self.db.products():
            self.product_tree.insert("", "end", iid=str(p["id"]), values=(
                p["code"], p["name"], p["unit"], fmt(p["purchase_rate"]),
                fmt(p["sales_rate"]), fmt(p["stock"]), fmt(p["min_stock"])))

    def load_product(self, _event=None):
        sel = self.product_tree.selection()
        if not sel:
            return
        p = self.db.con.execute("SELECT * FROM products WHERE id=?", (sel[0],)).fetchone()
        self.old_product_code = p["code"]
        for var, value in [(self.p_code,p["code"]), (self.p_name,p["name"]), (self.p_unit,p["unit"]),
                           (self.p_purchase,p["purchase_rate"]), (self.p_sales,p["sales_rate"]),
                           (self.p_stock,p["stock"]), (self.p_min,p["min_stock"])]:
            var.set(str(value))

    def clear_product_form(self):
        self.old_product_code = None
        for var, value in [(self.p_code,""),(self.p_name,""),(self.p_unit,"Pcs"),
                           (self.p_purchase,"0"),(self.p_sales,"0"),(self.p_stock,"0"),(self.p_min,"0")]:
            var.set(value)
        if hasattr(self, "product_tree"):
            self.product_tree.selection_remove(self.product_tree.selection())

    def save_product(self):
        try:
            code, name = self.p_code.get().strip(), self.p_name.get().strip()
            if not code or not name:
                raise ValueError("Product Code और Product Name जरूरी हैं।")
            self.db.save_product(code, name, self.p_unit.get().strip() or "Pcs",
                as_float(self.p_purchase.get(), "Purchase Rate"),
                as_float(self.p_sales.get(), "Sales Rate"),
                as_float(self.p_stock.get(), "Stock"),
                as_float(self.p_min.get(), "Minimum Stock"), self.old_product_code)
            self.clear_product_form()
            self.refresh_products()
            messagebox.showinfo("Product", "Product save हो गया।")
        except (ValueError, sqlite3.IntegrityError) as e:
            messagebox.showerror("Product", str(e))

    def delete_product(self):
        sel = self.product_tree.selection()
        if not sel:
            messagebox.showwarning("Select", "Delete करने के लिए product चुनें।")
            return
        code = self.product_tree.item(sel[0], "values")[0]
        if messagebox.askyesno("Confirm", f"{code} product delete करना है?"):
            try:
                self.db.delete_product(code)
                self.clear_product_form()
                self.refresh_products()
            except ValueError as e:
                messagebox.showerror("Delete", str(e))

    def show_records(self):
        self.clear_main()
        self.page_title("Invoice Records", "पुराने Sales और Purchase bills खोजें, देखें और जरूरत पर delete करें।")
        bar = tk.Frame(self.main, bg=self.BG)
        bar.pack(fill="x", padx=18, pady=8)
        self.search_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search_var, width=35).pack(side="left", padx=5)
        self.button(bar, "Search", self.refresh_records).pack(side="left", padx=5)
        self.button(bar, "Show All", lambda: (self.search_var.set(""), self.refresh_records()), "#63758a").pack(side="left", padx=5)
        self.button(bar, "View Bill", self.view_selected_invoice, self.GREEN).pack(side="left", padx=5)
        self.button(bar, "Delete Bill", self.delete_selected_invoice, self.RED).pack(side="left", padx=5)
        box = tk.Frame(self.main, bg=self.BG)
        box.pack(fill="both", expand=True, padx=18, pady=8)
        cols = ("voucher","kind","date","party","mobile","grand","paid","balance")
        self.record_tree = ttk.Treeview(box, columns=cols, show="headings")
        titles = ["Voucher No.","Type","Date","Party","Mobile","Grand Total","Paid","Balance"]
        widths = [105,95,110,210,120,120,110,110]
        for c, title, width in zip(cols, titles, widths):
            self.record_tree.heading(c, text=title)
            self.record_tree.column(c, width=width, anchor="center")
        self.record_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.record_tree.yview)
        self.record_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.record_tree.bind("<Double-1>", lambda _e: self.view_selected_invoice())
        self.refresh_records()

    def refresh_records(self):
        if not hasattr(self, "record_tree"):
            return
        self.record_tree.delete(*self.record_tree.get_children())
        for inv in self.db.invoices(self.search_var.get()):
            self.record_tree.insert("", "end", iid=str(inv["id"]), values=(
                inv["voucher"], inv["kind"], inv["inv_date"], inv["party"], inv["mobile"],
                fmt(inv["grand"]), fmt(inv["paid"]), fmt(inv["grand"]-inv["paid"])))

    def selected_invoice_id(self):
        sel = self.record_tree.selection()
        if not sel:
            messagebox.showwarning("Select", "पहले invoice record चुनें।")
            return None
        return int(sel[0])

    def view_selected_invoice(self):
        iid = self.selected_invoice_id()
        if iid:
            self.show_invoice_preview(iid)

    def delete_selected_invoice(self):
        iid = self.selected_invoice_id()
        if iid and messagebox.askyesno("Confirm Delete",
            "यह bill delete होगा और stock adjustment वापस होगा। क्या जारी रखें?"):
            try:
                self.db.delete_invoice(iid)
                self.refresh_records()
                messagebox.showinfo("Deleted", "Invoice delete हो गया।")
            except Exception as e:
                messagebox.showerror("Delete", str(e))

    def show_stock_report(self):
        self.clear_main()
        self.page_title("Stock Report", "सभी products का current stock और stock value देखें।")
        box = tk.Frame(self.main, bg=self.BG)
        box.pack(fill="both", expand=True, padx=18, pady=8)
        cols = ("code","name","unit","stock","purchase","value","minimum","status")
        tree = ttk.Treeview(box, columns=cols, show="headings")
        for c, title, width in zip(cols,
            ["Code","Product","Unit","Current Stock","Purchase Rate","Stock Value","Min Stock","Status"],
            [110,230,75,120,120,130,110,110]):
            tree.heading(c, text=title)
            tree.column(c, width=width, anchor="center")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        for p in self.db.products():
            status = "LOW STOCK" if p["stock"] <= p["min_stock"] else "OK"
            tree.insert("", "end", values=(p["code"],p["name"],p["unit"],fmt(p["stock"]),
                fmt(p["purchase_rate"]),fmt(p["stock"]*p["purchase_rate"]),
                fmt(p["min_stock"]),status))

    def show_shop_settings(self):
        self.clear_main()
        self.page_title("Shop Settings", "अपनी दुकान की जानकारी भरें। यह जानकारी बिल पर भी दिखाई जाएगी।")
        profile = self.db.get_store_profile()
        form = tk.LabelFrame(self.main, text="दुकान की जानकारी", bg="white",
                             padx=16, pady=14, font=("Segoe UI", 10, "bold"))
        form.pack(fill="x", padx=22, pady=12)

        self.shop_name_var = tk.StringVar(value=profile["shop_name"] or "")
        self.owner_name_var = tk.StringVar(value=profile["owner_name"] or "")
        self.shop_mobile_var = tk.StringVar(value=profile["mobile"] or "")
        self.shop_address_var = tk.StringVar(value=profile["address"] or "")
        self.shop_gstin_var = tk.StringVar(value=profile["gstin"] or "")
        self.shop_email_var = tk.StringVar(value=profile["email"] or "")

        fields = [
            ("दुकान का नाम", self.shop_name_var),
            ("मालिक का नाम", self.owner_name_var),
            ("मोबाइल नंबर", self.shop_mobile_var),
            ("दुकान का पता", self.shop_address_var),
            ("GST नंबर (यदि हो)", self.shop_gstin_var),
            ("ईमेल (यदि हो)", self.shop_email_var),
        ]
        for idx, (label, var) in enumerate(fields):
            row, col = divmod(idx, 2)
            tk.Label(form, text=label, bg="white", fg="#263c56",
                     font=("Segoe UI", 9, "bold")).grid(
                         row=row*2, column=col*2, sticky="w", padx=8, pady=(8, 2))
            ttk.Entry(form, textvariable=var, width=38).grid(
                row=row*2+1, column=col*2, sticky="w", padx=8, pady=(0, 8))

        self.button(form, "Save Shop Details", self.save_shop_settings, self.GREEN).grid(
            row=6, column=0, sticky="w", padx=8, pady=12)
        tk.Label(self.main,
                 text="नोट: अभी यह जानकारी इसी कंप्यूटर की स्थानीय database में सेव होगी। ऑनलाइन अलग-अलग दुकानों के Login की सुविधा अभी इस Desktop version में नहीं है।",
                 bg="#fff7df", fg="#725315", wraplength=850, justify="left",
                 padx=15, pady=12).pack(fill="x", padx=22, pady=8)

    def save_shop_settings(self):
        shop_name = self.shop_name_var.get().strip()
        if not shop_name:
            messagebox.showwarning("Shop Settings", "दुकान का नाम भरना जरूरी है।")
            return
        self.db.save_store_profile(
            shop_name, self.owner_name_var.get().strip(),
            self.shop_mobile_var.get().strip(), self.shop_address_var.get().strip(),
            self.shop_gstin_var.get().strip(), self.shop_email_var.get().strip())
        messagebox.showinfo("Shop Settings", "दुकान की जानकारी सेव हो गई।")

    def export_records(self):
        path = filedialog.asksaveasfilename(
            title="Export invoice records", defaultextension=".csv",
            filetypes=[("CSV file","*.csv")], initialfile="rk_invoice_records.csv")
        if not path:
            return
        try:
            rows = self.db.invoices("")
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Voucher","Type","Date","Party","Mobile","Subtotal",
                                 "Discount","Tax","Grand Total","Paid","Balance","Notes"])
                for r in rows:
                    writer.writerow([r["voucher"],r["kind"],r["inv_date"],r["party"],r["mobile"],
                        r["subtotal"],r["discount"],r["tax"],r["grand"],r["paid"],
                        r["grand"]-r["paid"],r["notes"]])
            messagebox.showinfo("Export", f"CSV file save हो गई:\n{path}")
        except Exception as e:
            messagebox.showerror("Export", str(e))

    def backup(self):
        path = filedialog.asksaveasfilename(
            title="Save database backup", defaultextension=".db",
            filetypes=[("SQLite database","*.db")], initialfile=f"rk_billing_backup_{date.today().isoformat()}.db")
        if not path:
            return
        try:
            self.db.con.commit()
            shutil.copy2(self.db.db_path, path)
            messagebox.showinfo("Backup", f"Backup सफलतापूर्वक save हुआ:\n{path}")
        except Exception as e:
            messagebox.showerror("Backup", str(e))


    def manage_accounts(self):
        self.clear_main()
        self.page_title("Manage Shop Accounts", "Admin यहाँ से नई दुकान का Username और Password बना सकता है।")
        box = tk.LabelFrame(self.main, text="Create New Shop Account", bg="white",
                            padx=18, pady=14, font=("Segoe UI", 10, "bold"))
        box.pack(fill="x", padx=22, pady=12)
        shop_var, user_var = tk.StringVar(), tk.StringVar()
        pass_var, confirm_var = tk.StringVar(), tk.StringVar()
        self.field(box, "Shop Name", shop_var, 0, 0, 25)
        self.field(box, "Username", user_var, 1, 0, 25)
        tk.Label(box, text="Password", bg="white").grid(row=2, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(box, textvariable=pass_var, show="*", width=27).grid(row=2, column=1, padx=6, pady=5, sticky="w")
        tk.Label(box, text="Confirm Password", bg="white").grid(row=3, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(box, textvariable=confirm_var, show="*", width=27).grid(row=3, column=1, padx=6, pady=5, sticky="w")

        def create():
            try:
                create_account(shop_var.get(), user_var.get(), pass_var.get(),
                               confirm_var.get(), role="shop", first_admin=False)
                messagebox.showinfo("Account Created", "नई दुकान का अकाउंट बन गया है।")
                shop_var.set(""); user_var.set(""); pass_var.set(""); confirm_var.set("")
                refresh()
            except Exception as exc:
                messagebox.showerror("Account Error", str(exc))

        tk.Button(box, text="Create Shop Account", command=create, bg=self.GREEN,
                  fg="white", relief="flat", padx=12, pady=7).grid(row=4, column=1, sticky="w", padx=6, pady=12)
        listing = tk.LabelFrame(self.main, text="Registered Accounts", bg="white",
                                padx=12, pady=10, font=("Segoe UI", 10, "bold"))
        listing.pack(fill="both", expand=True, padx=22, pady=8)
        tree = ttk.Treeview(listing, columns=("username", "shop", "role"), show="headings", height=10)
        for col, title, width in [("username", "Username", 180), ("shop", "Shop Name", 300), ("role", "Role", 100)]:
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w")
        tree.pack(fill="both", expand=True)

        def refresh():
            for item in tree.get_children():
                tree.delete(item)
            with sqlite3.connect(ACCOUNTS_DB) as con:
                for row in con.execute("SELECT username, shop_name, role FROM users ORDER BY username"):
                    tree.insert("", "end", values=row)
        refresh()

    def logout(self):
        if messagebox.askyesno("Logout", "क्या आप Logout करना चाहते हैं?"):
            self.db.close()
            self.destroy()
            LoginApp().mainloop()


    def toggle_fullscreen(self, _event=None):
        self.attributes("-fullscreen", not bool(self.attributes("-fullscreen")))

    def exit_fullscreen(self, _event=None):
        self.attributes("-fullscreen", False)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

    def close_app(self):
        self.db.close()
        self.destroy()



# -------------------- Local Username + Password Accounts --------------------
# This is a LOCAL desktop login system. It does not yet provide internet/cloud login.
ACCOUNTS_DB = os.path.join(APP_DIR, "rk_accounts.db")


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 240000).hex()


def ensure_accounts_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY,
        salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        db_file TEXT NOT NULL,
        shop_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")


def create_account(shop_name, username, password, confirm, role="shop", first_admin=False):
    shop_name, username = shop_name.strip(), username.strip().lower()
    if not shop_name or not username or not password:
        raise ValueError("Shop Name, Username और Password भरें।")
    if len(username) < 3 or not re.fullmatch(r"[a-z0-9_.-]+", username):
        raise ValueError("Username कम से कम 3 अक्षर का हो। केवल a-z, 0-9, dot, _ और - इस्तेमाल करें।")
    if len(password) < 8:
        raise ValueError("Password कम से कम 8 अक्षर का रखें।")
    if password != confirm:
        raise ValueError("दोनों Password एक जैसे नहीं हैं।")

    os.makedirs(APP_DIR, exist_ok=True)
    with sqlite3.connect(ACCOUNTS_DB) as con:
        ensure_accounts_table(con)
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("यह Username पहले से मौजूद है। दूसरा Username चुनें।")
        # The first Admin reuses the existing RK Billing database so old data remains accessible.
        db_file = os.path.basename(DB_PATH) if first_admin else f"shop_{uuid.uuid4().hex}.db"
        salt = secrets.token_hex(16)
        con.execute("""INSERT INTO users
            (username, salt, password_hash, role, db_file, shop_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, salt, password_hash(password, salt), role, db_file,
             shop_name, datetime.now().isoformat(timespec="seconds")))

    # Create the tenant database/profile for new shops; Admin keeps the existing database intact.
    tenant_path = os.path.join(APP_DIR, db_file)
    tenant_store = Store(tenant_path)
    if not first_admin:
        tenant_store.save_store_profile(shop_name, "", "", "", "", "")
    tenant_store.close()


def verify_login(username, password):
    username = username.strip().lower()
    if not os.path.exists(ACCOUNTS_DB):
        return None
    with sqlite3.connect(ACCOUNTS_DB) as con:
        con.row_factory = sqlite3.Row
        ensure_accounts_table(con)
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row and secrets.compare_digest(password_hash(password, row["salt"]), row["password_hash"]):
        return dict(row)
    return None


class LoginApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RK BILLING ERP - Login")
        self.geometry("510x500")
        self.minsize(450, 430)
        self.configure(bg="#edf3fa")
        self.resizable(True, True)
        self.show_login()

    def clear(self):
        for child in self.winfo_children():
            child.destroy()

    def heading(self, title, subtitle):
        tk.Label(self, text="RK BILLING ERP", bg="#062653", fg="#ffdc54",
                 font=("Segoe UI", 21, "bold"), pady=18).pack(fill="x")
        tk.Label(self, text=title, bg="#edf3fa", fg="#062653",
                 font=("Segoe UI", 17, "bold")).pack(pady=(20, 5))
        tk.Label(self, text=subtitle, bg="#edf3fa", fg="#53677e",
                 font=("Segoe UI", 9), wraplength=440).pack(pady=(0, 12))

    def field(self, parent, label, variable, row, show=None):
        tk.Label(parent, text=label, bg="white", fg="#263c56",
                 font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w", padx=8, pady=8)
        ttk.Entry(parent, textvariable=variable, width=30, show=show or "").grid(
            row=row, column=1, sticky="ew", padx=8, pady=8)

    def account_count(self):
        if not os.path.exists(ACCOUNTS_DB):
            return 0
        with sqlite3.connect(ACCOUNTS_DB) as con:
            ensure_accounts_table(con)
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def show_login(self):
        self.clear()
        self.heading("Login", "अपने अकाउंट में प्रवेश करने के लिए Username और Password डालें।")
        box = tk.Frame(self, bg="white", padx=18, pady=18)
        box.pack(fill="x", padx=35, pady=10)
        self.user_var, self.pass_var = tk.StringVar(), tk.StringVar()
        self.field(box, "Username", self.user_var, 0)
        self.field(box, "Password", self.pass_var, 1, "*")
        ttk.Button(box, text="Login", command=self.login).grid(row=2, column=1, sticky="ew", padx=8, pady=12)
        ttk.Button(self, text="नई दुकान का Registration", command=self.show_register).pack(pady=7)
        ttk.Button(self, text="पहली बार? Admin Account बनाएँ", command=self.show_admin_setup).pack(pady=4)
        tk.Label(self, text="यह संस्करण इसी कंप्यूटर पर Local Login के लिए है।",
                 bg="#edf3fa", fg="#53677e", font=("Segoe UI", 9)).pack(pady=9)

    def login(self):
        account = verify_login(self.user_var.get(), self.pass_var.get())
        if not account:
            messagebox.showerror("Login Failed", "Username या Password गलत है।")
            return
        db_path = os.path.join(APP_DIR, account["db_file"])
        self.destroy()
        app = ERP(db_path=db_path, username=account["username"],
                  role=account["role"], shop_name=account["shop_name"])
        app.mainloop()

    def show_register(self):
        self.clear()
        self.heading("Shop Registration", "अपनी दुकान के लिए अलग अकाउंट बनाएँ।")
        box = tk.Frame(self, bg="white", padx=18, pady=14)
        box.pack(fill="x", padx=28, pady=5)
        self.reg_shop, self.reg_user = tk.StringVar(), tk.StringVar()
        self.reg_pass, self.reg_confirm = tk.StringVar(), tk.StringVar()
        self.field(box, "Shop Name", self.reg_shop, 0)
        self.field(box, "Username", self.reg_user, 1)
        self.field(box, "Password (8+)", self.reg_pass, 2, "*")
        self.field(box, "Confirm Password", self.reg_confirm, 3, "*")

        def register():
            try:
                create_account(self.reg_shop.get(), self.reg_user.get(),
                               self.reg_pass.get(), self.reg_confirm.get(), role="shop")
                messagebox.showinfo("Registration", "अकाउंट बन गया। अब Username और Password से Login करें।")
                self.show_login()
            except Exception as exc:
                messagebox.showerror("Registration Error", str(exc))

        ttk.Button(box, text="Create Account", command=register).grid(row=4, column=1, sticky="ew", padx=8, pady=12)
        ttk.Button(self, text="Back to Login", command=self.show_login).pack(pady=6)

    def show_admin_setup(self):
        if self.account_count() > 0:
            messagebox.showinfo("Admin", "Admin Account पहले से मौजूद है। अब Login करें।")
            self.show_login()
            return
        self.clear()
        self.heading("Create Admin Account", "पहली बार Admin Username और Password बनाएँ। पुराना RK Billing database इसी अकाउंट से जुड़ेगा।")
        box = tk.Frame(self, bg="white", padx=18, pady=14)
        box.pack(fill="x", padx=28, pady=5)
        self.admin_shop, self.admin_user = tk.StringVar(value="My Shop"), tk.StringVar(value="admin")
        self.admin_pass, self.admin_confirm = tk.StringVar(), tk.StringVar()
        self.field(box, "Shop Name", self.admin_shop, 0)
        self.field(box, "Admin Username", self.admin_user, 1)
        self.field(box, "Password (8+)", self.admin_pass, 2, "*")
        self.field(box, "Confirm Password", self.admin_confirm, 3, "*")

        def setup():
            try:
                create_account(self.admin_shop.get(), self.admin_user.get(),
                               self.admin_pass.get(), self.admin_confirm.get(),
                               role="admin", first_admin=True)
                messagebox.showinfo("Admin Created", "Admin Account बन गया। अब Login करें।")
                self.show_login()
            except Exception as exc:
                messagebox.showerror("Admin Setup Error", str(exc))

        ttk.Button(box, text="Create Admin", command=setup).grid(row=4, column=1, sticky="ew", padx=8, pady=12)
        ttk.Button(self, text="Back to Login", command=self.show_login).pack(pady=6)


if __name__ == "__main__":
    app = LoginApp()
    if app.account_count() == 0:
        app.show_admin_setup()
    app.mainloop()
