import streamlit as st
import requests
import pandas as pd
import re
import os
from datetime import datetime

# --- Configuration ---
CSV_FILE = os.path.join(os.path.dirname(__file__), "tax.csv")
EXPORT_FILENAME = "israel_import_taxes.csv"

# API fallback (only used if CSV is not available)
API_URL = (
    "https://shaarolami-query.customs.mof.gov.il/CustomspilotWeb/SystemTables/api/"
    "GetTableData?tableName=ConcentratedTaxesView&includeMetadata=true"
)
API_COLUMN_MAPPING = {
    "ProductLevel1": "קטגוריה ראשית",
    "ProductLevel2": "קטגוריה משנית",
    "ProductLevel3": "מוצר",
    "Category1Taxes1": "עד 130$",
    "Category1Taxes2": "מ131$ עד 500$",
    "Category1Taxes3": "מ501$ עד 1000$",
    "Category1Taxes4": "מעל 1000$",
}

# Fixed column identifiers
COL_CAT = "קטגוריה ראשית"
COL_SUB = "קטגוריה משנית"
COL_PROD = "מוצר"


# ── Data Loading ────────────────────────────────────────────────


def detect_tax_tiers(df: pd.DataFrame) -> list[dict]:
    """Auto-detect tax tier columns and boundaries from CSV headers.

    Parses column names like 'עד 130$', 'מ131$ עד 500$', 'מעל 1000$'
    to extract the actual dollar thresholds.
    """
    tiers = []
    tier_cols = [c for c in df.columns if c not in [COL_CAT, COL_SUB, COL_PROD, "הערות"]]

    for col in tier_cols:
        # Parse "עד X$" -> max = X
        m = re.match(r"עד\s*(\d+)\$", col)
        if m:
            tiers.append({"max": int(m.group(1)), "col": col, "label": col})
            continue
        # Parse "מX$ עד Y$" -> max = Y
        m = re.match(r"מ-?(\d+)\$\s*עד\s*(\d+)\$", col)
        if m:
            tiers.append({"max": int(m.group(2)), "col": col, "label": col})
            continue
        # Parse "מעל X$" -> max = inf
        m = re.match(r"מעל\s*(\d+)\$", col)
        if m:
            tiers.append({"max": float("inf"), "col": col, "label": col})
            continue

    # Sort by max value
    tiers.sort(key=lambda t: t["max"])
    return tiers


def load_from_csv() -> tuple[pd.DataFrame, list[dict], str]:
    """Load tax data from local CSV file (primary source)."""
    df = pd.read_csv(CSV_FILE, encoding="utf-8-sig")
    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = df[col].str.strip()

    tiers = detect_tax_tiers(df)
    mod_time = os.path.getmtime(CSV_FILE)
    file_date = datetime.fromtimestamp(mod_time).strftime("%Y-%m-%d %H:%M")

    return df, tiers, file_date


def load_from_api() -> tuple[pd.DataFrame, list[dict], str]:
    """Fallback: load from API if CSV is not available."""
    response = requests.get(API_URL, timeout=30)
    response.raise_for_status()
    data = response.json()
    rows = data.get("Table", [])
    raw_df = pd.DataFrame(rows)

    keep = list(API_COLUMN_MAPPING.keys())
    df = raw_df[keep].copy()
    df = df.rename(columns=API_COLUMN_MAPPING)
    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = df[col].str.strip()

    tiers = detect_tax_tiers(df)
    metadata = data.get("Metadata", [{}])
    last_update = metadata[0].get("Column1", "לא ידוע") if metadata else "לא ידוע"

    return df, tiers, last_update


def load_data() -> tuple[pd.DataFrame, list[dict], str, str]:
    """Load data from CSV (primary) or API (fallback).

    Returns: (df, tiers, last_update, source)
    """
    if os.path.exists(CSV_FILE):
        df, tiers, file_date = load_from_csv()
        return df, tiers, file_date, "CSV"
    else:
        df, tiers, last_update = load_from_api()
        return df, tiers, last_update, "API (גיבוי)"


# ── Tax Helpers ─────────────────────────────────────────────────


def parse_tax_rate(tax_str: str) -> dict:
    """Parse a tax string into structured info."""
    if not tax_str or not isinstance(tax_str, str):
        return {"type": "empty", "rate": 0, "raw": str(tax_str)}
    tax_str = tax_str.strip()
    if tax_str == "פטור":
        return {"type": "exempt", "rate": 0, "raw": tax_str}
    match = re.match(r"^(?:שיעור\s+)?(\d+\.?\d*)\s*%\s*$", tax_str)
    if match:
        return {"type": "simple_percent", "rate": float(match.group(1)), "raw": tax_str}
    return {"type": "complex", "rate": 0, "raw": tax_str}


def get_tax_tier(price_usd: float, tiers: list[dict]) -> dict:
    """Determine which tax tier applies based on price."""
    for tier in tiers:
        if price_usd <= tier["max"]:
            return tier
    return tiers[-1]


def calculate_tax(price_usd: float, tax_info: dict) -> dict:
    """Calculate tax amount and total price."""
    if tax_info["type"] in ("exempt", "empty"):
        return {"tax_amount": 0, "total_price": price_usd, "calculable": True}
    elif tax_info["type"] == "simple_percent":
        tax_amount = price_usd * (tax_info["rate"] / 100)
        return {
            "tax_amount": round(tax_amount, 2),
            "total_price": round(price_usd + tax_amount, 2),
            "calculable": True,
        }
    return {"tax_amount": 0, "total_price": 0, "calculable": False}


def render_result_card(product_name, price_usd, tier, tax_info, result):
    """Render the result card HTML."""
    if tax_info["type"] == "exempt":
        st.markdown(
            f"""
            <div class="result-card">
                <h3>✅ {product_name}</h3>
                <div class="result-row">
                    <span class="result-label">מדרגת מס</span>
                    <span class="result-value">{tier['label']}</span>
                </div>
                <div class="result-row">
                    <span class="result-label">שיעור המס</span>
                    <span class="exempt-badge">פטור ממס ✓</span>
                </div>
                <div class="result-row">
                    <span class="result-label">מחיר המוצר</span>
                    <span class="result-value">${price_usd:,.2f}</span>
                </div>
                <div class="result-row result-total">
                    <span class="result-label">סה״כ לתשלום</span>
                    <span class="result-value">${price_usd:,.2f}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif result["calculable"]:
        st.markdown(
            f"""
            <div class="result-card">
                <h3>💰 {product_name}</h3>
                <div class="result-row">
                    <span class="result-label">מדרגת מס</span>
                    <span class="result-value">{tier['label']}</span>
                </div>
                <div class="result-row">
                    <span class="result-label">שיעור המס</span>
                    <span class="tax-badge">{tax_info['rate']}%</span>
                </div>
                <div class="result-row">
                    <span class="result-label">מחיר המוצר</span>
                    <span class="result-value">${price_usd:,.2f}</span>
                </div>
                <div class="result-row">
                    <span class="result-label">סכום המס</span>
                    <span class="result-value" style="color: #E87461 !important;">${result['tax_amount']:,.2f}</span>
                </div>
                <div class="result-row result-total">
                    <span class="result-label">סה״כ לתשלום</span>
                    <span class="result-value">${result['total_price']:,.2f}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div class="result-card">
                <h3>⚠️ {product_name}</h3>
                <div class="result-row">
                    <span class="result-label">מדרגת מס</span>
                    <span class="result-value">{tier['label']}</span>
                </div>
                <div class="result-row">
                    <span class="result-label">מחיר המוצר</span>
                    <span class="result-value">${price_usd:,.2f}</span>
                </div>
                <div class="complex-tax-note">
                    <strong>⚙️ נוסחת מס מורכבת:</strong><br>
                    {tax_info['raw']}<br><br>
                    מוצר זה כולל מס מורכב שאינו ניתן לחישוב אוטומטי.
                    מומלץ לפנות לרשות המסים לקבלת חישוב מדויק.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ── Streamlit App ───────────────────────────────────────────────

st.set_page_config(
    page_title="מחשבון מסי יבוא - ישראל",
    page_icon="🇮🇱",
    layout="wide",
)

css_path = os.path.join(os.path.dirname(__file__), "style.css")
with open(css_path, encoding="utf-8") as f:
    st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

# ── Header ──
st.markdown("# 🇮🇱 מחשבון מסי יבוא לישראל")
st.markdown(
    '<p class="app-subtitle">'
    "כלי לחישוב מהיר של מסי יבוא על מוצרים המיובאים לישראל · "
    'מבוסס על נתוני <a href="https://www.gov.il/apps/taxes/importTable/" target="_blank">רשות המסים</a>'
    "</p>",
    unsafe_allow_html=True,
)

# ── Load data ──
if "df" not in st.session_state:
    with st.spinner("טוען נתונים..."):
        try:
            df, tiers, last_update, source = load_data()
            st.session_state["df"] = df
            st.session_state["tiers"] = tiers
            st.session_state["last_update"] = last_update
            st.session_state["source"] = source
            st.session_state["load_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            st.error(f"❌ שגיאה בטעינת הנתונים: {e}")
            st.stop()

# ── Tabs ──
tab_calc, tab_table = st.tabs(["🧮 מחשבון מס יבוא", "📋 טבלת מסים מלאה"])

# ════════════════════════════════════════════════════════════════
#  TAB 1: CALCULATOR
# ════════════════════════════════════════════════════════════════
with tab_calc:
    df = st.session_state["df"]
    tiers = st.session_state["tiers"]

    st.markdown("### בחר מוצר וחשב את המס")

    # ── Cascading Dropdowns ──
    col_cat, col_sub, col_prod = st.columns(3)

    pre_cat = st.session_state.pop("nav_cat", None)
    pre_sub = st.session_state.pop("nav_sub", None)
    pre_prod = st.session_state.pop("nav_prod", None)

    with col_cat:
        categories = sorted(df[COL_CAT].unique().tolist())
        cat_idx = 0
        if pre_cat and pre_cat in categories:
            cat_idx = categories.index(pre_cat)
        selected_cat = st.selectbox("📂 קטגוריה ראשית", categories, index=cat_idx, key="calc_cat")

    with col_sub:
        sub_df = df[df[COL_CAT] == selected_cat]
        sub_categories = sorted(sub_df[COL_SUB].unique().tolist())
        sub_idx = 0
        if pre_sub and pre_sub in sub_categories:
            sub_idx = sub_categories.index(pre_sub)
        selected_sub = st.selectbox("📁 קטגוריה משנית", sub_categories, index=sub_idx, key="calc_sub")

    with col_prod:
        prod_df = sub_df[sub_df[COL_SUB] == selected_sub]
        products = sorted(prod_df[COL_PROD].unique().tolist())
        prod_idx = 0
        if pre_prod and pre_prod in products:
            prod_idx = products.index(pre_prod)
        selected_prod = st.selectbox("🏷️ מוצר", products, index=prod_idx, key="calc_prod")

    st.divider()

    # ── Price Input ──
    col_price, _ = st.columns([1, 2])
    with col_price:
        price_usd = st.number_input(
            "💵 מחיר המוצר (בדולרים)",
            min_value=0.0, max_value=1_000_000.0,
            value=0.0, step=1.0, format="%.2f",
            key="calc_price",
        )

    # ── Calculate ──
    if price_usd > 0:
        product_row = prod_df[prod_df[COL_PROD] == selected_prod].iloc[0]
        tier = get_tax_tier(price_usd, tiers)
        tax_str = str(product_row[tier["col"]]).strip()
        tax_info = parse_tax_rate(tax_str)
        result = calculate_tax(price_usd, tax_info)

        st.markdown("### 📊 תוצאת החישוב")
        render_result_card(selected_prod, price_usd, tier, tax_info, result)

        with st.expander("📋 הצג את כל מדרגות המס למוצר זה"):
            tier_data = []
            for t in tiers:
                val = str(product_row[t["col"]]).strip()
                info = parse_tax_rate(val)
                if info["type"] == "exempt":
                    display_val = "פטור ✓"
                elif info["type"] == "simple_percent":
                    display_val = f'{info["rate"]}%'
                else:
                    display_val = val if val else "—"
                tier_data.append({"שיעור מס": display_val, "מדרגה": t["label"]})
            st.table(pd.DataFrame(tier_data))
    else:
        st.markdown(
            """
            <div style="text-align: center; padding: 40px 20px; color: #A0AEC0;">
                <div style="font-size: 2.5rem; margin-bottom: 10px;">💵</div>
                <div style="font-size: 1.05rem; font-weight: 500;">
                    הזן את מחיר המוצר בדולרים כדי לחשב את המס
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ════════════════════════════════════════════════════════════════
#  TAB 2: FULL TAX TABLE
# ════════════════════════════════════════════════════════════════
with tab_table:
    df = st.session_state["df"]
    tiers = st.session_state["tiers"]
    source = st.session_state["source"]

    st.markdown(
        f"📊 **{len(df)} מוצרים** · "
        f"📁 מקור: **{source}** · "
        f"🕒 `{st.session_state.get('last_update', '')}` · "
        f"📥 נטען ב: `{st.session_state.get('load_time', '')}`"
    )

    st.divider()

    # ── Search & Filter ──
    col_search, col_cat_f, col_sub_f = st.columns([2, 1, 1])

    with col_search:
        search_term = st.text_input("🔍 חיפוש מוצר", placeholder="הקלד שם מוצר...", key="tbl_search")

    with col_cat_f:
        cats = ["הכל"] + sorted(df[COL_CAT].unique().tolist())
        sel_cat = st.selectbox("📂 קטגוריה ראשית", cats, key="tbl_cat")

    with col_sub_f:
        if sel_cat != "הכל":
            subs = ["הכל"] + sorted(df[df[COL_CAT] == sel_cat][COL_SUB].unique().tolist())
        else:
            subs = ["הכל"] + sorted(df[COL_SUB].unique().tolist())
        sel_sub = st.selectbox("📁 קטגוריה משנית", subs, key="tbl_sub")

    # Apply filters
    filtered = df.copy()
    if search_term:
        mask = filtered.apply(
            lambda row: row.astype(str).str.contains(search_term, case=False).any(), axis=1
        )
        filtered = filtered[mask]
    if sel_cat != "הכל":
        filtered = filtered[filtered[COL_CAT] == sel_cat]
    if sel_sub != "הכל":
        filtered = filtered[filtered[COL_SUB] == sel_sub]

    st.markdown(f"**מציג {len(filtered)} מתוך {len(df)} מוצרים**")

    # ── Reverse columns for RTL display ──
    display_filtered = filtered[filtered.columns[::-1]]
    display_filtered = display_filtered.reset_index(drop=True)

    # ── Clickable Table ──
    if len(display_filtered) > 0:
        event = st.dataframe(
            display_filtered,
            use_container_width=True,
            height=400,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="tbl_data",
        )

        selected_rows = event.selection.rows if event.selection else []

        if selected_rows and selected_rows[0] < len(display_filtered):
            row_idx = selected_rows[0]
            sel_row = filtered.iloc[row_idx]
            prod_name = sel_row[COL_PROD]
            cat_name = sel_row[COL_CAT]
            sub_name = sel_row[COL_SUB]

            st.markdown(
                f"""
                <div class="inline-calc-box">
                    <h4>🧮 חישוב מס עבור:</h4>
                    <span class="selected-product-tag">{prod_name}</span>
                    <span style="color: #718096; font-size: 0.9rem; margin-right: 8px;">
                        {cat_name} ◂ {sub_name}
                    </span>
                </div>
                """,
                unsafe_allow_html=True,
            )

            inline_price = st.number_input(
                "💵 הזן מחיר בדולרים",
                min_value=0.0, max_value=1_000_000.0,
                value=0.0, step=1.0, format="%.2f",
                key="tbl_inline_price",
            )

            if inline_price > 0:
                tier = get_tax_tier(inline_price, tiers)
                tax_str = str(sel_row[tier["col"]]).strip()
                tax_info = parse_tax_rate(tax_str)
                result = calculate_tax(inline_price, tax_info)
                render_result_card(prod_name, inline_price, tier, tax_info, result)
        else:
            st.markdown(
                """
                <div style="text-align: center; padding: 24px; color: #A0AEC0;
                     border: 1px dashed #E2E8F0; border-radius: 12px; margin-top: 12px;">
                    <div style="font-size: 1rem; font-weight: 500;">
                        👆 לחץ על שורה בטבלה כדי לחשב את המס עבור המוצר
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    else:
        st.dataframe(display_filtered, use_container_width=True, height=400, hide_index=True)

    # ── Export ──
    st.divider()
    col_dl, _ = st.columns(2)
    with col_dl:
        csv_data = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="⬇️ הורדת CSV",
            data=csv_data,
            file_name=EXPORT_FILENAME,
            mime="text/csv",
            use_container_width=True,
            key="dl_csv",
        )
