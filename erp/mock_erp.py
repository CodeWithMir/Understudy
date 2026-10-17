"""Pillar 2 - Target ERP Simulation.
Replicates enterprise latency, validation rules, and authentication headers.
Mirrors Revision 2.0 Pillar 2 and Revision 3.0 Section 3.2.1.
Also serves a live Accounts Payable Web Portal for Mode B (Playwright) browser interaction.
"""
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Header, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Enterprise Mock ERP System", version="2.0.0")

class InvoiceModel(BaseModel):
    vendor_name: str
    total_amount: float
    invoice_date: str
    invoice_notes: Optional[str] = ""
    uploaded_from_portal: Optional[str] = "Portal1"

LEDGER_DATABASE: List[Dict[str, Any]] = [
    {
        "trx_id": "TRX-1000",
        "vendor_name": "Global Office Logistics",
        "total_amount": 320.00,
        "invoice_date": "2026-09-01",
        "invoice_notes": "Initial balance setup",
        "uploaded_from_portal": "Portal1",
        "status": "SETTLED"
    }
]

async def verify_auth_token(x_erp_token: Optional[str] = Header(None)):
    if x_erp_token != "secret-enterprise-token-xyz":
        raise HTTPException(status_code=401, detail="Unauthorized ERP access.")
    return x_erp_token

@app.get("/api/erp/validate")
async def validate_transaction(vendor: str, amount: float, auth: str = Depends(verify_auth_token)):
    """Simulates enterprise validation against contract registries."""
    await asyncio.sleep(0.35)  # Replicate database latency
    if amount > 5000.00:
        return {
            "is_valid": False,
            "status_code": "EXCEEDS_CEILING",
            "message": f"Invoice amount ${amount:,.2f} exceeds standard $5,000 threshold."
        }
    return {
        "is_valid": True,
        "status_code": "APPROVED",
        "message": "Transaction cleared."
    }

@app.post("/api/erp/invoices")
async def commit_invoice(invoice: InvoiceModel, auth: str = Depends(verify_auth_token)):
    """Simulates ACID-compliant insertion into financial accounts."""
    await asyncio.sleep(0.5)
    record = invoice.model_dump()
    record["trx_id"] = f"TRX-{len(LEDGER_DATABASE) + 1001}"
    record["status"] = "COMMITTED"
    if not record.get("uploaded_from_portal"):
        record["uploaded_from_portal"] = "Portal1"
    if record.get("invoice_notes") is None:
        record["invoice_notes"] = ""
    LEDGER_DATABASE.append(record)
    return {
        "status": "SUCCESS",
        "trx_id": record["trx_id"],
        "committed_amount": invoice.total_amount,
        "invoice_notes": record["invoice_notes"],
        "uploaded_from_portal": record["uploaded_from_portal"]
    }

@app.get("/api/erp/ledger")
async def get_ledger():
    """Returns the current ERP transaction ledger."""
    return {"ledger": LEDGER_DATABASE, "count": len(LEDGER_DATABASE)}

@app.delete("/api/erp/ledger")
@app.post("/api/erp/ledger/clear")
async def clear_ledger():
    """Clears all transactions from the ERP database ledger."""
    LEDGER_DATABASE.clear()
    return {"status": "SUCCESS", "message": "ERP transaction ledger cleared successfully.", "count": 0}

# The portals' styles and fonts are served locally (compiled Tailwind + OFL fonts in erp/static), so the demo
# portals look the same without internet access - no CDN, no Google Fonts.
_STATIC_DIR = __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "static")
_STATIC_TYPES = {".css": "text/css", ".woff2": "font/woff2"}


@app.get("/erp-static/{path:path}", include_in_schema=False)
async def erp_static(path: str):
    import os
    from fastapi.responses import FileResponse
    full = os.path.realpath(os.path.join(_STATIC_DIR, path))
    ext = os.path.splitext(full)[1]
    if not full.startswith(os.path.realpath(_STATIC_DIR) + os.sep) or ext not in _STATIC_TYPES or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(full, media_type=_STATIC_TYPES[ext], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/portal", response_class=HTMLResponse)
@app.get("/portal1", response_class=HTMLResponse)
async def accounts_payable_portal():
    """Interactive Accounts Payable UI for Mode B Playwright execution."""
    ledger_rows = "".join([
        f"""<tr class="border-b border-slate-700/50 hover:bg-slate-800/40 transition-colors">
            <td class="py-2.5 px-3 font-mono text-xs text-sky-400">{row.get('trx_id', 'N/A')}</td>
            <td class="py-2.5 px-3 text-sm text-slate-200">{row.get('vendor_name', 'N/A')}</td>
            <td class="py-2.5 px-3 font-mono text-xs text-slate-400">{row.get('invoice_date', 'N/A')}</td>
            <td class="py-2.5 px-3 font-mono text-sm font-semibold text-emerald-400 text-right">${float(row.get('total_amount', 0)):,.2f}</td>
            <td class="py-2.5 px-3 text-xs text-slate-300 max-w-[180px] truncate" title="{row.get('invoice_notes', '') or '—'}">{row.get('invoice_notes', '') or '—'}</td>
            <td class="py-2.5 px-3 text-center"><span class="px-2 py-0.5 rounded text-[11px] font-mono font-semibold {'bg-purple-500/20 text-purple-300 border border-purple-500/30' if row.get('uploaded_from_portal') == 'Portal2' else 'bg-sky-500/20 text-sky-300 border border-sky-500/30'}">{row.get('uploaded_from_portal', 'Portal1')}</span></td>
            <td class="py-2.5 px-3 text-center"><span class="px-2 py-0.5 rounded text-[11px] font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">COMMITTED</span></td>
        </tr>""" for row in reversed(LEDGER_DATABASE[-10:])
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Enterprise ERP 2.0 - Accounts Payable Portal</title>
  <link rel="stylesheet" href="/erp-static/portal.css">
  <style>
    body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}
    code, pre, .font-mono {{ font-family: 'JetBrains Mono', monospace; }}
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen p-6 antialiased">
  <div class="max-w-5xl mx-auto space-y-6">
    <!-- Header -->
    <header class="flex items-center justify-between border-b border-slate-800 pb-4">
      <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-lg bg-gradient-to-tr from-sky-500 to-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-sky-500/20">
          AP
        </div>
        <div>
          <h1 class="text-lg font-bold text-white tracking-wide">Enterprise ERP — Accounts Payable Portal</h1>
          <p class="text-xs text-slate-400">Target System Simulation for Understudy Mode B Automation</p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse mr-1.5"></span>
          LEDGER ONLINE (PORT 8000)
        </span>
      </div>
    </header>

    <div class="grid grid-cols-1 md:grid-cols-12 gap-6">
      <!-- Invoice Entry Form -->
      <div class="md:col-span-5 bg-slate-900/90 border border-slate-800 rounded-xl p-5 shadow-xl">
        <h2 class="text-sm font-bold text-white uppercase tracking-wider mb-1 flex items-center gap-2">
          <span>Manual Invoice Intake</span>
        </h2>
        <p class="text-xs text-slate-400 mb-4">FIN-SOP-402 Standard Ledger Entry Interface</p>

        <form id="invoice-form" onsubmit="event.preventDefault(); submitInvoiceForm();" class="space-y-4">
          <div>
            <label for="vendor_name" class="block text-xs font-semibold text-slate-300 mb-1">Vendor Name</label>
            <input type="text" id="vendor_name" name="vendor_name" placeholder="e.g. Acme Industrial Supplies"
              class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 transition-all" required>
          </div>

          <div>
            <label for="invoice_date" class="block text-xs font-semibold text-slate-300 mb-1">Invoice Date</label>
            <input type="date" id="invoice_date" name="invoice_date"
              class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 transition-all" required>
          </div>

          <div>
            <label for="total_amount" class="block text-xs font-semibold text-slate-300 mb-1">Total Amount ($ USD)</label>
            <input type="number" step="0.01" id="total_amount" name="total_amount" placeholder="e.g. 450.00"
              class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 transition-all font-mono" required>
          </div>

          <div>
            <label for="invoice_notes" class="block text-xs font-semibold text-slate-300 mb-1">Invoice Notes</label>
            <input type="text" id="invoice_notes" name="invoice_notes" placeholder="e.g. Net 30 vendor terms"
              class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 transition-all">
          </div>

          <div id="status-box" class="hidden p-3 rounded-lg text-xs"></div>

          <div class="pt-2">
            <button type="submit" id="submit-btn" name="Submit Invoice"
              class="w-full bg-sky-600 hover:bg-sky-500 text-white font-semibold text-sm py-2.5 px-4 rounded-lg shadow-lg shadow-sky-600/30 transition-all active:scale-[0.98] flex items-center justify-center gap-2">
              <span>Submit Invoice</span>
            </button>
          </div>
        </form>
      </div>

      <!-- Ledger Table -->
      <div class="md:col-span-7 bg-slate-900/90 border border-slate-800 rounded-xl p-5 shadow-xl flex flex-col">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-sm font-bold text-white uppercase tracking-wider">Committed AP Ledger</h2>
          <button onclick="location.reload()" class="text-xs text-sky-400 hover:text-sky-300 transition-colors">Refresh</button>
        </div>
        <div class="overflow-x-auto flex-1">
          <table class="w-full text-left border-collapse">
            <thead>
              <tr class="border-b border-slate-800 text-[11px] font-bold text-slate-400 uppercase tracking-wider">
                <th class="py-2 px-3">TRX ID</th>
                <th class="py-2 px-3">Vendor</th>
                <th class="py-2 px-3">Date</th>
                <th class="py-2 px-3 text-right">Amount</th>
                <th class="py-2 px-3">Invoice Notes</th>
                <th class="py-2 px-3 text-center">Uploaded From Portal</th>
                <th class="py-2 px-3 text-center">Status</th>
              </tr>
            </thead>
            <tbody id="ledger-body">
              {ledger_rows}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <script>
    async function submitInvoiceForm() {{
      const vendor = document.getElementById("vendor_name").value;
      const date = document.getElementById("invoice_date").value || new Date().toISOString().split('T')[0];
      const amount = parseFloat(document.getElementById("total_amount").value);
      const notes = document.getElementById("invoice_notes") ? document.getElementById("invoice_notes").value : "";
      const statusBox = document.getElementById("status-box");
      const submitBtn = document.getElementById("submit-btn");

      submitBtn.disabled = true;
      submitBtn.innerText = "Validating...";

      try {{
        const res = await fetch("/api/erp/invoices", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
            "X-ERP-Token": "secret-enterprise-token-xyz"
          }},
          body: JSON.stringify({{ vendor_name: vendor, total_amount: amount, invoice_date: date, invoice_notes: notes, uploaded_from_portal: "Portal1" }})
        }});
        const data = await res.json();
        if (res.ok) {{
          statusBox.className = "p-3 rounded-lg text-xs bg-emerald-500/10 border border-emerald-500/30 text-emerald-400";
          statusBox.innerHTML = `Success! Committed as <strong>${{data.trx_id}}</strong> for $${{data.committed_amount.toFixed(2)}}`;
          statusBox.classList.remove("hidden");
          setTimeout(() => location.reload(), 1200);
        }} else {{
          statusBox.className = "p-3 rounded-lg text-xs bg-rose-500/10 border border-rose-500/30 text-rose-400";
          statusBox.innerHTML = `Error: ${{data.detail || 'Failed to commit'}}`;
          statusBox.classList.remove("hidden");
        }}
      }} catch (err) {{
        statusBox.className = "p-3 rounded-lg text-xs bg-rose-500/10 border border-rose-500/30 text-rose-400";
        statusBox.innerHTML = `Network Error: ${{err.message}}`;
        statusBox.classList.remove("hidden");
      }} finally {{
        submitBtn.disabled = false;
        submitBtn.innerText = "Submit Invoice";
      }}
    }}
  </script>
</body>
</html>"""


@app.post("/api/erp/portal2/upload")
async def portal2_invoice_upload(
    file: Optional[UploadFile] = File(None),
    company_title: Optional[str] = Form(None),
    posting_date: Optional[str] = Form(None),
    payable_amount: Optional[float] = Form(None),
    cost_center: Optional[str] = Form(None),
    invoice_notes: Optional[str] = Form(None),
    uploaded_from_portal: Optional[str] = Form("Portal2")
):
    """Direct file upload & intelligent ingestion endpoint for Portal 2."""
    await asyncio.sleep(0.4)
    vendor = company_title or "Apex Modern Technologies Corp"
    amount = payable_amount or 580.00
    date = posting_date or datetime.now().strftime("%Y-%m-%d")
    filename = "direct_upload.pdf"

    if file:
        filename = file.filename
        content = await file.read()
        text_content = content.decode("utf-8", errors="ignore")
        if "Acme" in text_content or "Acme" in filename:
            vendor = "Acme Industrial Supplies"
            amount = 450.00
        elif "Apex" in text_content or "Apex" in filename:
            vendor = "Apex Server Architecture Ltd"
            amount = 8500.00
        elif "Global Office" in text_content or "Global" in filename:
            vendor = "Global Office Logistics"
            amount = 320.00
        elif not company_title:
            vendor = f"{filename.split('.')[0].replace('_', ' ').replace('-', ' ').title()}"
            amount = 642.50

    record = {
        "trx_id": f"TRX-{len(LEDGER_DATABASE) + 1001}",
        "vendor_name": vendor,
        "total_amount": float(amount),
        "invoice_date": date,
        "invoice_notes": invoice_notes or "",
        "uploaded_from_portal": uploaded_from_portal or "Portal2",
        "status": "COMMITTED",
        "source": "PORTAL_2_UPLOAD",
        "filename": filename,
    }
    LEDGER_DATABASE.append(record)
    return {
        "status": "SUCCESS",
        "trx_id": record["trx_id"],
        "vendor_name": vendor,
        "total_amount": float(amount),
        "invoice_date": date,
        "invoice_notes": record["invoice_notes"],
        "uploaded_from_portal": record["uploaded_from_portal"],
        "filename": filename,
        "message": f"Document '{filename}' successfully ingested and committed to General Ledger as {record['trx_id']}."
    }


@app.get("/portal2", response_class=HTMLResponse)
async def accounts_payable_portal_v2():
    """Modernized Cloud ERP 360 (Portal 2) with migrated UI and alternative DOM selectors for Resiliency testing."""
    ledger_rows = "".join([
        f"""<tr class="border-b border-indigo-950/60 hover:bg-indigo-950/30 transition-colors">
            <td class="py-2.5 px-3 font-mono text-xs text-cyan-400 font-bold">{row.get('trx_id', 'N/A')}</td>
            <td class="py-2.5 px-3 text-sm text-slate-100 font-medium">{row.get('vendor_name', 'N/A')}</td>
            <td class="py-2.5 px-3 font-mono text-xs text-slate-400">{row.get('invoice_date', 'N/A')}</td>
            <td class="py-2.5 px-3 font-mono text-sm font-semibold text-emerald-400 text-right">${float(row.get('total_amount', 0)):,.2f}</td>
            <td class="py-2.5 px-3 text-xs text-slate-300 max-w-[180px] truncate" title="{row.get('invoice_notes', '') or '—'}">{row.get('invoice_notes', '') or '—'}</td>
            <td class="py-2.5 px-3 text-center"><span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold {'bg-purple-500/20 text-purple-300 border border-purple-500/30' if row.get('uploaded_from_portal') == 'Portal2' else 'bg-sky-500/20 text-sky-300 border border-sky-500/30'}">{row.get('uploaded_from_portal', 'Portal2')}</span></td>
            <td class="py-2.5 px-3 text-center">
              <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold tracking-wide {'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30' if row.get('source') == 'PORTAL_2_UPLOAD' else 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'}">
                {row.get('source', 'COMMITTED')}
              </span>
            </td>
        </tr>""" for row in reversed(LEDGER_DATABASE[-12:])
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CloudERP 360 — Accounts Payable Portal v2.5 (Portal 2)</title>
  <link rel="stylesheet" href="/erp-static/portal.css">
  <style>
    body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}
    code, pre, .font-mono {{ font-family: 'JetBrains Mono', monospace; }}
  </style>
</head>
<body class="bg-[#070b14] text-slate-100 min-h-screen p-6 antialiased">
  <div class="max-w-6xl mx-auto space-y-6">
    <!-- Top Enterprise Navigation -->
    <header class="flex items-center justify-between border-b border-indigo-900/50 pb-4">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-cyan-500 via-teal-400 to-emerald-500 flex items-center justify-center font-black text-slate-950 shadow-lg shadow-cyan-500/20 text-lg">
          360
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="text-lg font-bold text-white tracking-wide">CloudERP 360 Enterprise</h1>
            <span class="text-[10px] font-bold px-2 py-0.5 rounded bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 font-mono">
              PORTAL 2 • MIGRATED UI
            </span>
            <span class="text-[10px] font-bold px-2 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/30 font-mono">
              RESILIENCY TARGET
            </span>
          </div>
          <p class="text-xs text-slate-400">Accounts Payable Voucher Ingestion &amp; General Ledger Reconciliation</p>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <a href="/portal" class="text-xs px-3 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-700 transition-all flex items-center gap-1.5 font-medium">
          <span>&larr; Switch to Legacy Portal 1</span>
        </a>
        <span class="inline-flex items-center px-3 py-1.5 rounded-full text-xs font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 font-mono">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse mr-2"></span>
          PORTAL 2 ONLINE (:8000)
        </span>
      </div>
    </header>

    <!-- Resiliency Banner -->
    <div class="bg-gradient-to-r from-cyan-950/60 via-indigo-950/60 to-purple-950/60 border border-cyan-800/40 rounded-xl p-4 flex items-center justify-between text-xs text-cyan-200">
      <div class="flex items-center gap-2.5">
        <span class="text-base font-bold text-cyan-400">&#9889;</span>
        <div>
          <strong class="text-white">Resiliency &amp; Self-Healing Test Target:</strong>
          <span class="text-cyan-200/90 ml-1">
            This portal has an altered DOM structure, different CSS selectors (<code class="bg-black/50 px-1 py-0.5 rounded text-cyan-300">#company_title</code>, <code class="bg-black/50 px-1 py-0.5 rounded text-cyan-300">#payable_amount</code>, <code class="bg-black/50 px-1 py-0.5 rounded text-cyan-300">#post-voucher-button</code>), and a direct document drag-and-drop intake system.
          </span>
        </div>
      </div>
      <span class="font-mono text-[10px] bg-cyan-900/60 px-2.5 py-1 rounded text-cyan-300 border border-cyan-700/50 whitespace-nowrap">
        Zero-Break Guarantee
      </span>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
      <!-- Left Column: Document Upload + Voucher Intake Form -->
      <div class="lg:col-span-5 space-y-5">
        <!-- 1. Direct Invoice Document Upload Dropzone -->
        <div class="bg-slate-900/90 border border-indigo-900/40 rounded-2xl p-5 shadow-xl space-y-3">
          <div class="flex items-center justify-between">
            <h2 class="text-xs font-bold text-white uppercase tracking-wider flex items-center gap-1.5">
              <span>1. Direct Invoice Document Upload</span>
            </h2>
            <span class="text-[10px] text-cyan-400 bg-cyan-950/60 px-2 py-0.5 rounded font-mono">
              Portal 2 Ingest
            </span>
          </div>

          <input type="file" id="portal2-file-input" onchange="handlePortal2FileSelect(event)" accept=".pdf,.png,.jpg,.jpeg,.txt,.json" class="hidden" />

          <div
            id="portal2-dropzone"
            onclick="document.getElementById('portal2-file-input').click()"
            ondragover="event.preventDefault(); this.classList.add('border-cyan-400', 'bg-cyan-950/30');"
            ondragleave="this.classList.remove('border-cyan-400', 'bg-cyan-950/30');"
            ondrop="event.preventDefault(); this.classList.remove('border-cyan-400', 'bg-cyan-950/30'); handlePortal2Drop(event);"
            class="border-2 border-dashed border-indigo-800/70 hover:border-cyan-500/70 bg-[#0b101e] rounded-xl p-5 text-center cursor-pointer transition-all group"
          >
            <div class="w-10 h-10 rounded-full bg-cyan-950/80 text-cyan-400 flex items-center justify-center mx-auto mb-2 group-hover:scale-110 transition-transform">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>
            </div>
            <p class="text-xs font-semibold text-white">Drag &amp; drop invoice document here</p>
            <p class="text-[11px] text-slate-400 mt-0.5">or click to browse (.pdf, .png, .jpg, .txt)</p>
            <div class="mt-2.5 inline-flex items-center gap-1.5 text-[10px] text-cyan-300 bg-cyan-950/70 px-2.5 py-1 rounded border border-cyan-800/50 font-medium">
              <span>Upload to Portal 2</span>
            </div>
          </div>

          <div id="upload-status" class="hidden p-3 rounded-lg text-xs font-mono"></div>
        </div>

        <!-- 2. Altered Accounts Payable Form (Resiliency Selectors) -->
        <div class="bg-slate-900/90 border border-indigo-900/40 rounded-2xl p-5 shadow-xl">
          <div class="flex items-center justify-between mb-3 pb-2 border-b border-indigo-950">
            <div>
              <h2 class="text-xs font-bold text-white uppercase tracking-wider">2. Voucher Intake Form</h2>
              <p class="text-[11px] text-slate-400">Altered field names testing self-healing locator cascades</p>
            </div>
            <span class="text-[10px] font-mono text-purple-300 bg-purple-950/60 px-2 py-0.5 rounded border border-purple-800/40">
              DOM v2.5
            </span>
          </div>

          <form id="portal2-form" onsubmit="event.preventDefault(); submitPortal2ManualForm();" class="space-y-3.5">
            <div>
              <label for="company_title" class="block text-xs font-semibold text-slate-300 mb-1">
                Vendor Name (Supplier Organization)
              </label>
              <input
                type="text"
                id="company_title"
                name="company_title"
                data-testid="supplier-input"
                placeholder="e.g. Acme Industrial Supplies"
                class="w-full bg-[#0b101e] border border-indigo-900/60 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 font-medium transition-all"
                required
              />
            </div>

            <div class="grid grid-cols-2 gap-3">
              <div>
                <label for="posting_date" class="block text-xs font-semibold text-slate-300 mb-1">
                  Invoice Date (Posting Period)
                </label>
                <input
                  type="date"
                  id="posting_date"
                  name="posting_date"
                  data-testid="date-picker"
                  class="w-full bg-[#0b101e] border border-indigo-900/60 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 font-mono transition-all"
                  required
                />
              </div>

              <div>
                <label for="payable_amount" class="block text-xs font-semibold text-slate-300 mb-1">
                  Total Amount ($ USD Gross)
                </label>
                <input
                  type="number"
                  step="0.01"
                  id="payable_amount"
                  name="payable_amount"
                  data-testid="amount-input"
                  placeholder="e.g. 450.00"
                  class="w-full bg-[#0b101e] border border-indigo-900/60 rounded-lg px-3 py-2 text-xs text-emerald-400 font-mono font-bold focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 transition-all"
                  required
                />
              </div>
            </div>

            <div>
              <label for="cost_center_code" class="block text-xs font-semibold text-slate-300 mb-1">
                Cost Center / GL Allocation Code
              </label>
              <input
                type="text"
                id="cost_center_code"
                name="cost_center_code"
                data-testid="cost-center"
                placeholder="CC-4100-FIN-AP"
                value="CC-4100-FIN-AP"
                class="w-full bg-[#0b101e] border border-indigo-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300 font-mono focus:outline-none focus:border-cyan-500"
              />
            </div>

            <div>
              <label for="invoice_notes" class="block text-xs font-semibold text-slate-300 mb-1">
                Invoice Notes (Voucher Remarks)
              </label>
              <input
                type="text"
                id="invoice_notes"
                name="invoice_notes"
                data-testid="invoice-notes"
                placeholder="e.g. Net 30 vendor terms"
                class="w-full bg-[#0b101e] border border-indigo-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-cyan-500 font-medium"
              />
            </div>

            <div id="manual-status-box" class="hidden p-3 rounded-lg text-xs font-mono"></div>

            <div class="pt-1">
              <button
                type="submit"
                id="post-voucher-button"
                data-testid="post-button"
                name="Submit Invoice (Post Voucher)"
                class="w-full bg-gradient-to-r from-cyan-600 via-teal-600 to-emerald-600 hover:from-cyan-500 hover:to-emerald-500 text-slate-950 font-extrabold text-xs py-2.5 px-4 rounded-lg shadow-lg shadow-cyan-600/20 transition-all active:scale-[0.98] flex items-center justify-center gap-1.5"
              >
                <span>Submit Invoice (Post Voucher)</span>
              </button>
            </div>
          </form>
        </div>
      </div>

      <!-- Right Column: Live General Ledger Table -->
      <div class="lg:col-span-7 bg-slate-900/90 border border-indigo-900/40 rounded-2xl p-5 shadow-xl flex flex-col space-y-4">
        <div class="flex items-center justify-between pb-3 border-b border-indigo-950">
          <div>
            <h2 class="text-sm font-bold text-white uppercase tracking-wider">Synchronized General Ledger</h2>
            <p class="text-xs text-slate-400">Live transactions committed across Portal 1, Portal 2, &amp; All Engines</p>
          </div>
          <button onclick="location.reload()" class="text-xs bg-slate-800 hover:bg-slate-700 text-cyan-300 px-3 py-1 rounded border border-slate-700 transition-all font-mono">
            Refresh Ledger
          </button>
        </div>

        <div class="overflow-x-auto flex-1">
          <table class="w-full text-left border-collapse text-xs">
            <thead>
              <tr class="border-b border-indigo-900/60 text-[11px] font-bold text-slate-400 uppercase tracking-wider">
                <th class="py-2.5 px-3">TRX ID</th>
                <th class="py-2.5 px-3">Supplier Organization</th>
                <th class="py-2.5 px-3">Posting Date</th>
                <th class="py-2.5 px-3 text-right">Gross Total</th>
                <th class="py-2.5 px-3">Invoice Notes</th>
                <th class="py-2.5 px-3 text-center">Uploaded From Portal</th>
                <th class="py-2.5 px-3 text-center">Status / Channel</th>
              </tr>
            </thead>
            <tbody id="portal2-ledger-body">
              {ledger_rows}
            </tbody>
          </table>
        </div>

        <div class="bg-[#0b101e] border border-indigo-900/40 rounded-xl p-3 flex items-center justify-between text-xs text-slate-400">
          <div class="flex items-center gap-2">
            <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
            <span>ACID-Compliant General Ledger commits are instantaneous</span>
          </div>
          <span class="font-mono text-cyan-300">Unified DB Engine</span>
        </div>
      </div>
    </div>
  </div>

  <script>
    async function uploadInvoiceFile(file) {{
      const statusBox = document.getElementById("upload-status");
      statusBox.className = "p-3 rounded-lg text-xs bg-cyan-950/80 border border-cyan-500/40 text-cyan-300 block";
      statusBox.innerHTML = `Uploading '${{file.name}}' to Portal 2 Ingestion Engine...`;

      const formData = new FormData();
      formData.append("file", file);
      formData.append("uploaded_from_portal", "Portal2");

      try {{
        const res = await fetch("/api/erp/portal2/upload", {{
          method: "POST",
          body: formData
        }});
        const data = await res.json();
        if (res.ok) {{
          statusBox.className = "p-3 rounded-lg text-xs bg-emerald-950/80 border border-emerald-500/40 text-emerald-300 block";
          statusBox.innerHTML = `Success! Document <strong>${{data.filename}}</strong> posted as <strong>${{data.trx_id}}</strong> for $${{data.total_amount.toFixed(2)}} (${{data.vendor_name}})`;
          
          // Auto fill the manual form with extracted fields
          document.getElementById("company_title").value = data.vendor_name;
          document.getElementById("posting_date").value = data.invoice_date;
          document.getElementById("payable_amount").value = data.total_amount;

          setTimeout(() => location.reload(), 1400);
        }} else {{
          statusBox.className = "p-3 rounded-lg text-xs bg-rose-950/80 border border-rose-500/40 text-rose-300 block";
          statusBox.innerHTML = `Upload Error: ${{data.detail || 'Failed to process'}}`;
        }}
      }} catch (err) {{
        statusBox.className = "p-3 rounded-lg text-xs bg-rose-950/80 border border-rose-500/40 text-rose-300 block";
        statusBox.innerHTML = `Network Error: ${{err.message}}`;
      }}
    }}

    function handlePortal2FileSelect(event) {{
      const file = event.target.files?.[0];
      if (file) uploadInvoiceFile(file);
    }}

    function handlePortal2Drop(event) {{
      const file = event.dataTransfer.files?.[0];
      if (file) uploadInvoiceFile(file);
    }}

    async function submitPortal2ManualForm() {{
      const vendor = document.getElementById("company_title").value;
      const date = document.getElementById("posting_date").value || new Date().toISOString().split('T')[0];
      const amount = parseFloat(document.getElementById("payable_amount").value);
      const costCenter = document.getElementById("cost_center_code").value;
      const invoiceNotes = document.getElementById("invoice_notes") ? document.getElementById("invoice_notes").value : "";
      const statusBox = document.getElementById("manual-status-box");
      const submitBtn = document.getElementById("post-voucher-button");

      submitBtn.disabled = true;
      submitBtn.innerText = "Posting to General Ledger...";

      const formData = new FormData();
      formData.append("company_title", vendor);
      formData.append("posting_date", date);
      formData.append("payable_amount", amount);
      formData.append("cost_center", costCenter);
      formData.append("invoice_notes", invoiceNotes);
      formData.append("uploaded_from_portal", "Portal2");

      try {{
        const res = await fetch("/api/erp/portal2/upload", {{
          method: "POST",
          body: formData
        }});
        const data = await res.json();
        if (res.ok) {{
          statusBox.className = "p-3 rounded-lg text-xs bg-emerald-950/80 border border-emerald-500/40 text-emerald-300 block";
          statusBox.innerHTML = `Success! Posted voucher <strong>${{data.trx_id}}</strong> for $${{data.total_amount.toFixed(2)}} to General Ledger.`;
          setTimeout(() => location.reload(), 1300);
        }} else {{
          statusBox.className = "p-3 rounded-lg text-xs bg-rose-950/80 border border-rose-500/40 text-rose-300 block";
          statusBox.innerHTML = `Error: ${{data.detail || 'Posting failed'}}`;
        }}
      }} catch (err) {{
        statusBox.className = "p-3 rounded-lg text-xs bg-rose-950/80 border border-rose-500/40 text-rose-300 block";
        statusBox.innerHTML = `Network Error: ${{err.message}}`;
      }} finally {{
        submitBtn.disabled = false;
        submitBtn.innerText = "Submit Invoice (Post Voucher)";
      }}
    }}
  </script>
</body>
</html>"""



# ---------------------------------------------------------------------------------------------------------------
# Portal 3 (9.0) - "Shifting Input Forms": an RPA-Challenge-style page (https://rpachallenge.com) for testing
# automation that must survive layout changes. Every render gives the 7 inputs NEW random ids / names and a NEW
# order; the label is a sibling of its input (not linked with for=). After every Submit the form re-shuffles.
# 10 rounds of data are offered as a CSV, like the original's Excel.
# ---------------------------------------------------------------------------------------------------------------
PORTAL3_FIELDS = [("First Name", "labelFirstName"), ("Last Name", "labelLastName"), ("Company Name", "labelCompanyName"),
                  ("Role in Company", "labelRole"), ("Address", "labelAddress"), ("Email", "labelEmail"),
                  ("Phone Number", "labelPhone")]
PORTAL3_DATA = [
    ["John", "Smith", "IT Solutions", "Analyst", "98 North Road", "jsmith@itsolutions.co.uk", "40716543298"],
    ["Jane", "Dorsey", "MediCare", "Medical Engineer", "11 Crown Street", "jdorsey@mc.com", "40791345621"],
    ["Albert", "Kipling", "Waterfront", "Accountant", "22 Guild Street", "kipling@waterfront.com", "40735416854"],
    ["Michael", "Robertson", "MediCare", "IT Specialist", "17 Farburn Terrace", "mrobertson@mc.com", "40733652145"],
    ["Doug", "Derrick", "Timepath Inc.", "Analyst", "99 Shire Oak Road", "dderrick@timepath.co.uk", "40799885412"],
    ["Jessie", "Marlowe", "Aperture Inc.", "Scientist", "27 Cheshire Street", "jmarlowe@aperture.us", "40733154268"],
    ["Stan", "Hamm", "Sugarwell", "Advisor", "10 Dam Road", "shamm@sugarwell.org", "40712462257"],
    ["Michelle", "Norton", "Aperture Inc.", "Scientist", "13 White Rabbit Street", "mnorton@aperture.us", "40731254562"],
    ["Stacy", "Shelby", "TechDev", "HR Manager", "19 Pineapple Boulevard", "sshelby@techdev.com", "40741785214"],
    ["Lara", "Palmer", "Timepath Inc.", "Programmer", "87 Orange Street", "lpalmer@timepath.co.uk", "40731653845"],
]
PORTAL3_SUBMISSIONS: List[Dict[str, Any]] = []


@app.get("/api/erp/portal3/data.csv", include_in_schema=False)
async def portal3_data():
    from fastapi.responses import PlainTextResponse
    head = ",".join(label for label, _ in PORTAL3_FIELDS)
    rows = "\n".join(",".join(r) for r in PORTAL3_DATA)
    return PlainTextResponse(head + "\n" + rows + "\n", media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=challenge.csv"})


@app.post("/api/erp/portal3/submit")
async def portal3_submit(request: Request):
    body = await request.json()
    rec = {str(k)[:40]: str(v)[:200] for k, v in (body or {}).items() if k in {l for l, _ in PORTAL3_FIELDS}}
    rec["round"] = len(PORTAL3_SUBMISSIONS) + 1
    rec["submitted_at"] = datetime.utcnow().isoformat()
    PORTAL3_SUBMISSIONS.append(rec)
    return {"ok": True, "round": rec["round"]}


@app.get("/api/erp/portal3/submissions")
async def portal3_submissions():
    return {"submissions": PORTAL3_SUBMISSIONS[-50:], "count": len(PORTAL3_SUBMISSIONS)}


@app.post("/api/erp/portal3/reset")
async def portal3_reset():
    PORTAL3_SUBMISSIONS.clear()
    return {"ok": True}


@app.get("/portal3", response_class=HTMLResponse)
async def shifting_forms_portal():
    """RPA-Challenge-style shifting form (labels are siblings of inputs; ids, names and order change every render)."""
    import json as _json
    fields = _json.dumps(PORTAL3_FIELDS)
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portal 3 - Shifting Input Forms</title>
<style>
 body {{ margin:0; font-family: system-ui, sans-serif; background:#f4f6f9; color:#1f2937; }}
 header {{ background:#1976d2; color:#fff; padding:14px 24px; font-size:22px; }}
 main {{ display:flex; gap:24px; padding:24px; }}
 aside {{ width:260px; background:#0d47a1; color:#e3f2fd; padding:16px; border-radius:6px; font-size:13px; line-height:1.5; }}
 aside a {{ color:#fff; }}
 .form {{ flex:1; background:#fff; padding:24px; border-radius:6px; }}
 .row {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px 32px; max-width:640px; }}
 .form-group label {{ display:block; font-size:11px; color:#6b7280; }}
 .form-group input {{ width:100%; border:0; border-bottom:1px solid #9ca3af; padding:6px 0; font-size:14px; outline:none; }}
 .btn {{ margin-top:18px; background:#e65100; color:#fff; border:0; padding:8px 22px; font-weight:600; cursor:pointer; }}
 .msg {{ margin-top:14px; font-size:14px; color:#065f46; }}
</style></head>
<body>
<header>Portal 3 &middot; Shifting Input Forms</header>
<main>
 <aside>
  <b>Instructions</b>
  <p>1. Fill the form with one record and click Submit.</p>
  <p>2. Beware! The fields change position (and their ids) every time the page loads and after every submission,
  so the automation must find each field by what it is, not where it is.</p>
  <p><a href="/api/erp/portal3/data.csv">Download the data (CSV, 10 rows)</a></p>
 </aside>
 <section class="form">
  <form id="challenge-form" autocomplete="off" onsubmit="return submitRound(event)">
   <div class="row" id="fields"></div>
   <input type="submit" value="Submit" class="btn">
  </form>
  <div class="msg" id="message" role="status"></div>
 </section>
</main>
<script>
const FIELDS = {fields};
function rid() {{ return Math.random().toString(36).slice(2, 7) + Math.random().toString(36).slice(2, 5); }}
function render() {{
  const order = FIELDS.slice().sort(() => Math.random() - 0.5);
  const box = document.getElementById('fields');
  box.innerHTML = '';
  for (const [label, reflect] of order) {{
    const id = rid();
    const wrap = document.createElement('rpa1-field');
    wrap.setAttribute('ng-reflect-dictionary-value', label);
    wrap.innerHTML = '<div class="form-group"><label>' + label + '</label>' +
      '<input ng-reflect-name="' + reflect + '" id="' + id + '" name="' + rid() + '" class="form-control" type="text"></div>';
    box.appendChild(wrap);
  }}
}}
async function submitRound(ev) {{
  ev.preventDefault();
  const rec = {{}};
  document.querySelectorAll('#fields .form-group').forEach(g => {{
    rec[g.querySelector('label').textContent.trim()] = g.querySelector('input').value;
  }});
  const res = await fetch('/api/erp/portal3/submit', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                                                       body: JSON.stringify(rec) }});
  const out = await res.json();
  document.getElementById('message').textContent = 'Round ' + out.round + ' submitted: ' + (rec['First Name'] || '') +
    ' ' + (rec['Last Name'] || '');
  render();
  return false;
}}
render();
</script>
</body></html>""")
