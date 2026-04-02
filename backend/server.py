from fastapi import FastAPI, APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import jwt
import io
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT Config
JWT_SECRET = os.environ.get('JWT_SECRET', 'fastcopy_secret_key_2024')
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Predefined users
USERS = {
    "admin": {"password": "admin", "role": "admin", "name": "Administrador"},
    "Luis": {"password": "Luis", "role": "viewer", "name": "Luis"}
}

# Categories
INCOME_CATEGORIES = ["Bondi", "Neurolik", "Mis terapias", "Ventas"]
EXPENSE_CATEGORIES = ["Transporte", "Chucherias", "aseo y belleza", "Comida"]

# Create the main app
app = FastAPI()
api_router = APIRouter(prefix="/api")
security = HTTPBearer()

# Models
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    token: str
    user: dict

class TransactionBase(BaseModel):
    amount: float
    description: str
    category: str
    date: Optional[str] = None

class TransactionCreate(TransactionBase):
    pass

class Transaction(TransactionBase):
    id: str
    type: str
    created_at: str
    created_by: str
    category: Optional[str] = None

class BalanceSummary(BaseModel):
    total_income: float
    total_expenses: float
    balance: float
    period: str

# Auth helpers
def create_token(username: str, role: str):
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {"username": payload["sub"], "role": payload["role"]}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

def require_admin(user: dict = Depends(verify_token)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Acceso denegado. Solo administradores")
    return user

# Auth routes
@api_router.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    user = USERS.get(request.username)
    if not user or user["password"] != request.password:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    
    token = create_token(request.username, user["role"])
    return {
        "token": token,
        "user": {
            "username": request.username,
            "role": user["role"],
            "name": user["name"]
        }
    }

@api_router.get("/auth/me")
async def get_current_user(user: dict = Depends(verify_token)):
    user_data = USERS.get(user["username"])
    return {
        "username": user["username"],
        "role": user["role"],
        "name": user_data["name"] if user_data else user["username"]
    }

# Categories routes
@api_router.get("/categories/income")
async def get_income_categories(user: dict = Depends(verify_token)):
    return {"categories": INCOME_CATEGORIES}

@api_router.get("/categories/expenses")
async def get_expense_categories(user: dict = Depends(verify_token)):
    return {"categories": EXPENSE_CATEGORIES}

# Income routes
@api_router.post("/income", response_model=Transaction)
async def create_income(transaction: TransactionCreate, user: dict = Depends(require_admin)):
    if transaction.category not in INCOME_CATEGORIES:
        raise HTTPException(status_code=400, detail="Categoría de ingreso inválida")
    
    doc = {
        "id": str(uuid.uuid4()),
        "type": "income",
        "amount": transaction.amount,
        "description": transaction.description,
        "category": transaction.category,
        "date": transaction.date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": user["username"]
    }
    await db.transactions.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api_router.get("/income", response_model=List[Transaction])
async def get_income(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: dict = Depends(verify_token)
):
    query = {"type": "income"}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        if "date" in query:
            query["date"]["$lte"] = end_date
        else:
            query["date"] = {"$lte": end_date}
    
    transactions = await db.transactions.find(query, {"_id": 0}).sort("date", -1).to_list(1000)
    return transactions

@api_router.delete("/income/{transaction_id}")
async def delete_income(transaction_id: str, user: dict = Depends(require_admin)):
    result = await db.transactions.delete_one({"id": transaction_id, "type": "income"})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Ingreso no encontrado")
    return {"message": "Ingreso eliminado"}

# Expense routes
@api_router.post("/expenses", response_model=Transaction)
async def create_expense(transaction: TransactionCreate, user: dict = Depends(require_admin)):
    if transaction.category not in EXPENSE_CATEGORIES:
        raise HTTPException(status_code=400, detail="Categoría de gasto inválida")
    
    doc = {
        "id": str(uuid.uuid4()),
        "type": "expense",
        "amount": transaction.amount,
        "description": transaction.description,
        "category": transaction.category,
        "date": transaction.date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": user["username"]
    }
    await db.transactions.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api_router.get("/expenses", response_model=List[Transaction])
async def get_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: dict = Depends(verify_token)
):
    query = {"type": "expense"}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        if "date" in query:
            query["date"]["$lte"] = end_date
        else:
            query["date"] = {"$lte": end_date}
    
    transactions = await db.transactions.find(query, {"_id": 0}).sort("date", -1).to_list(1000)
    return transactions

@api_router.delete("/expenses/{transaction_id}")
async def delete_expense(transaction_id: str, user: dict = Depends(require_admin)):
    result = await db.transactions.delete_one({"id": transaction_id, "type": "expense"})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Gasto no encontrado")
    return {"message": "Gasto eliminado"}

# Summary routes
@api_router.get("/summary")
async def get_summary(
    period: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: dict = Depends(verify_token)
):
    today = datetime.now(timezone.utc).date()
    
    if period == "daily":
        start = today.strftime("%Y-%m-%d")
        end = start
    elif period == "weekly":
        start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
    elif period == "monthly":
        start = today.replace(day=1).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
    elif start_date and end_date:
        start = start_date
        end = end_date
    else:
        start = None
        end = None
    
    income_query = {"type": "income"}
    expense_query = {"type": "expense"}
    
    if start and end:
        income_query["date"] = {"$gte": start, "$lte": end}
        expense_query["date"] = {"$gte": start, "$lte": end}
    
    income_pipeline = [
        {"$match": income_query},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    expense_pipeline = [
        {"$match": expense_query},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    
    income_result = await db.transactions.aggregate(income_pipeline).to_list(1)
    expense_result = await db.transactions.aggregate(expense_pipeline).to_list(1)
    
    total_income = income_result[0]["total"] if income_result else 0
    total_expenses = expense_result[0]["total"] if expense_result else 0
    
    return {
        "total_income": total_income,
        "total_expenses": total_expenses,
        "balance": total_income - total_expenses,
        "period": period,
        "start_date": start,
        "end_date": end
    }

@api_router.get("/chart-data")
async def get_chart_data(
    period: str = "monthly",
    user: dict = Depends(verify_token)
):
    today = datetime.now(timezone.utc).date()
    
    if period == "daily":
        # Last 7 days
        dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    elif period == "weekly":
        # Last 4 weeks
        dates = []
        for i in range(3, -1, -1):
            week_start = today - timedelta(days=today.weekday() + 7*i)
            dates.append(week_start.strftime("%Y-%m-%d"))
    else:
        # Last 6 months
        dates = []
        for i in range(5, -1, -1):
            month_date = today.replace(day=1) - timedelta(days=30*i)
            dates.append(month_date.strftime("%Y-%m"))
    
    chart_data = []
    
    for date_str in dates:
        if period == "daily":
            query_start = date_str
            query_end = date_str
            label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m")
        elif period == "weekly":
            query_start = date_str
            end_date = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=6)
            query_end = end_date.strftime("%Y-%m-%d")
            label = f"Sem {datetime.strptime(date_str, '%Y-%m-%d').strftime('%d/%m')}"
        else:
            query_start = date_str + "-01"
            next_month = datetime.strptime(query_start, "%Y-%m-%d").replace(day=28) + timedelta(days=4)
            query_end = (next_month.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
            label = datetime.strptime(query_start, "%Y-%m-%d").strftime("%b")
        
        income_pipeline = [
            {"$match": {"type": "income", "date": {"$gte": query_start, "$lte": query_end}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
        ]
        expense_pipeline = [
            {"$match": {"type": "expense", "date": {"$gte": query_start, "$lte": query_end}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
        ]
        
        income_result = await db.transactions.aggregate(income_pipeline).to_list(1)
        expense_result = await db.transactions.aggregate(expense_pipeline).to_list(1)
        
        chart_data.append({
            "name": label,
            "ingresos": income_result[0]["total"] if income_result else 0,
            "gastos": expense_result[0]["total"] if expense_result else 0
        })
    
    return chart_data

# Export routes
@api_router.get("/export/excel")
async def export_excel(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: dict = Depends(verify_token)
):
    query = {}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        if "date" in query:
            query["date"]["$lte"] = end_date
        else:
            query["date"] = {"$lte": end_date}
    
    transactions = await db.transactions.find(query, {"_id": 0}).sort("date", -1).to_list(1000)
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Transacciones"
    
    # Headers
    ws.append(["Fecha", "Tipo", "Categoría", "Descripción", "Monto (USD)"])
    
    for t in transactions:
        tipo = "Ingreso" if t["type"] == "income" else "Gasto"
        monto = t["amount"] if t["type"] == "income" else -t["amount"]
        ws.append([t["date"], tipo, t.get("category", "Sin categoría"), t["description"], monto])
    
    # Summary
    ws.append([])
    total_income = sum(t["amount"] for t in transactions if t["type"] == "income")
    total_expenses = sum(t["amount"] for t in transactions if t["type"] == "expense")
    ws.append(["", "", "Total Ingresos:", total_income])
    ws.append(["", "", "Total Gastos:", total_expenses])
    ws.append(["", "", "Balance:", total_income - total_expenses])
    
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=fastcopy_reporte.xlsx"}
    )

@api_router.get("/export/pdf")
async def export_pdf(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: dict = Depends(verify_token)
):
    query = {}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        if "date" in query:
            query["date"]["$lte"] = end_date
        else:
            query["date"] = {"$lte": end_date}
    
    transactions = await db.transactions.find(query, {"_id": 0}).sort("date", -1).to_list(1000)
    
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []
    
    # Title
    elements.append(Paragraph("Reporte Financiero - Fastcopy", styles['Title']))
    elements.append(Spacer(1, 20))
    
    # Date range
    date_text = "Todas las fechas"
    if start_date and end_date:
        date_text = f"Desde {start_date} hasta {end_date}"
    elif start_date:
        date_text = f"Desde {start_date}"
    elif end_date:
        date_text = f"Hasta {end_date}"
    elements.append(Paragraph(f"Período: {date_text}", styles['Normal']))
    elements.append(Spacer(1, 20))
    
    # Table data
    data = [["Fecha", "Tipo", "Categoría", "Descripción", "Monto (USD)"]]
    for t in transactions:
        tipo = "Ingreso" if t["type"] == "income" else "Gasto"
        monto = f"{t['amount']:,.2f}" if t["type"] == "income" else f"-{t['amount']:,.2f}"
        data.append([t["date"], tipo, t.get("category", "Sin categoría"), t["description"][:30], monto])
    
    if len(data) > 1:
        table = Table(data, colWidths=[60, 50, 80, 150, 80])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        elements.append(table)
    
    # Summary
    elements.append(Spacer(1, 30))
    total_income = sum(t["amount"] for t in transactions if t["type"] == "income")
    total_expenses = sum(t["amount"] for t in transactions if t["type"] == "expense")
    balance = total_income - total_expenses
    
    elements.append(Paragraph(f"<b>Total Ingresos:</b> {total_income:,.2f} USD", styles['Normal']))
    elements.append(Paragraph(f"<b>Total Gastos:</b> {total_expenses:,.2f} USD", styles['Normal']))
    elements.append(Paragraph(f"<b>Balance:</b> {balance:,.2f} USD", styles['Heading2']))
    
    doc.build(elements)
    output.seek(0)
    
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=fastcopy_reporte.pdf"}
    )

# All transactions
@api_router.get("/transactions", response_model=List[Transaction])
async def get_all_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: dict = Depends(verify_token)
):
    query = {}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        if "date" in query:
            query["date"]["$lte"] = end_date
        else:
            query["date"] = {"$lte": end_date}
    
    transactions = await db.transactions.find(query, {"_id": 0}).sort("date", -1).to_list(1000)
    return transactions

# Health check
@api_router.get("/")
async def root():
    return {"message": "Fastcopy API running", "status": "ok"}

# Include router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
