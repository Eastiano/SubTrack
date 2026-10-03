import os
import uuid
from datetime import datetime, timedelta, timezone
from html import escape

import resend
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

load_dotenv()

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

database_url = os.getenv("DATABASE_URL", "sqlite:///subscriptions.db")

# Some providers still expose postgres://; SQLAlchemy expects postgresql://.
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)

COOKIE_NAME = "subtrack_user_id"


class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    anonymous_user_id = db.Column(db.String(100), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False, default="Other")
    price = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default="NGN")
    billing_cycle = db.Column(db.String(20), nullable=False)
    renewal_date = db.Column(db.Date, nullable=False, index=True)
    reminder_days = db.Column(db.Integer, nullable=False, default=7)
    reminder_email = db.Column(db.String(150), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="Active")
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ReminderLog(db.Model):
    __tablename__ = "reminder_logs"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "renewal_date",
            "reminder_days",
            name="uq_subscription_renewal_reminder",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    renewal_date = db.Column(db.Date, nullable=False)
    reminder_days = db.Column(db.Integer, nullable=False)
    recipient_email = db.Column(db.String(150), nullable=False)
    resend_email_id = db.Column(db.String(100), nullable=True)
    sent_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def get_anonymous_user():
    user_id = request.cookies.get(COOKIE_NAME)
    return user_id or str(uuid.uuid4())


def attach_user_cookie(response, user_id):
    if not request.cookies.get(COOKIE_NAME):
        response.set_cookie(
            COOKIE_NAME,
            user_id,
            max_age=60 * 60 * 24 * 365 * 5,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
        )
    return response


def calculate_next_renewal(renewal_date, billing_cycle, today=None):
    today = today or datetime.now(timezone.utc).date()

    while renewal_date < today:
        if billing_cycle == "Weekly":
            renewal_date += timedelta(days=7)
        elif billing_cycle == "Monthly":
            renewal_date += relativedelta(months=1)
        elif billing_cycle == "Yearly":
            renewal_date += relativedelta(years=1)
        else:
            break

    return renewal_date


def normalize_renewal_dates(subscriptions, today=None):
    today = today or datetime.now(timezone.utc).date()
    changed = False

    for subscription in subscriptions:
        if subscription.status != "Active":
            continue

        new_date = calculate_next_renewal(
            subscription.renewal_date,
            subscription.billing_cycle,
            today,
        )

        if new_date != subscription.renewal_date:
            subscription.renewal_date = new_date
            changed = True

    if changed:
        db.session.commit()


def monthly_equivalent(subscription):
    if subscription.billing_cycle == "Monthly":
        return subscription.price
    if subscription.billing_cycle == "Yearly":
        return subscription.price / 12
    if subscription.billing_cycle == "Weekly":
        return (subscription.price * 52) / 12
    return subscription.price


def format_money(currency, amount):
    symbols = {
        "NGN": "₦",
        "USD": "$",
        "GBP": "£",
        "EUR": "€",
    }
    symbol = symbols.get(currency, f"{currency} ")
    return f"{symbol}{amount:,.2f}"


def reminder_subject(subscription, days_left):
    if days_left == 0:
        return f"{subscription.name} renews today"
    if days_left == 1:
        return f"{subscription.name} renews tomorrow"
    return f"{subscription.name} renews in {days_left} days"


def send_subscription_reminder(subscription, days_left):
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("RESEND_FROM_EMAIL")

    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not from_email:
        raise RuntimeError("RESEND_FROM_EMAIL is not configured")

    resend.api_key = api_key

    safe_name = escape(subscription.name)
    safe_category = escape(subscription.category or "Subscription")
    safe_price = escape(format_money(subscription.currency, subscription.price))
    safe_cycle = escape(subscription.billing_cycle)
    safe_date = subscription.renewal_date.strftime("%d %B %Y")

    if days_left == 0:
        timing = "renews today"
    elif days_left == 1:
        timing = "renews tomorrow"
    else:
        timing = f"renews in {days_left} days"

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#111827;line-height:1.6">
      <h2 style="margin-bottom:8px">SubTrack renewal reminder</h2>
      <p style="color:#4b5563;margin-top:0">Your <strong>{safe_name}</strong> subscription {timing}.</p>
      <div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;padding:18px;margin:20px 0">
        <p style="margin:4px 0"><strong>Subscription:</strong> {safe_name}</p>
        <p style="margin:4px 0"><strong>Category:</strong> {safe_category}</p>
        <p style="margin:4px 0"><strong>Amount:</strong> {safe_price}</p>
        <p style="margin:4px 0"><strong>Billing cycle:</strong> {safe_cycle}</p>
        <p style="margin:4px 0"><strong>Renewal date:</strong> {safe_date}</p>
      </div>
      <p style="color:#6b7280;font-size:13px">You received this message because this email address was added to a subscription reminder in SubTrack.</p>
    </div>
    """

    params = {
        "from": from_email,
        "to": [subscription.reminder_email],
        "subject": reminder_subject(subscription, days_left),
        "html": html_body,
    }

    result = resend.Emails.send(params)

    # Resend currently returns an object/dict containing an id. Support both.
    if isinstance(result, dict):
        return result.get("id")
    return getattr(result, "id", None)


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    user_id = get_anonymous_user()
    today = datetime.now(timezone.utc).date()

    subscriptions = (
        Subscription.query.filter_by(anonymous_user_id=user_id)
        .order_by(Subscription.renewal_date)
        .all()
    )

    normalize_renewal_dates(subscriptions, today)

    active_count = 0
    renewing_soon = 0
    monthly_totals = {}
    yearly_totals = {}

    for subscription in subscriptions:
        if subscription.status != "Active":
            continue

        active_count += 1
        days_left = (subscription.renewal_date - today).days

        if 0 <= days_left <= 7:
            renewing_soon += 1

        currency = subscription.currency
        monthly_cost = monthly_equivalent(subscription)
        monthly_totals[currency] = monthly_totals.get(currency, 0) + monthly_cost
        yearly_totals[currency] = yearly_totals.get(currency, 0) + monthly_cost * 12

    response = make_response(
        render_template(
            "index.html",
            subscriptions=subscriptions,
            today=today,
            active_count=active_count,
            renewing_soon=renewing_soon,
            monthly_totals=monthly_totals,
            yearly_totals=yearly_totals,
        )
    )
    return attach_user_cookie(response, user_id)


@app.route("/add", methods=["GET", "POST"])
def add_subscription():
    user_id = get_anonymous_user()

    if request.method == "POST":
        subscription = Subscription(
            anonymous_user_id=user_id,
            name=request.form["name"].strip(),
            category=request.form["category"],
            price=float(request.form["price"]),
            currency=request.form["currency"],
            billing_cycle=request.form["billing_cycle"],
            renewal_date=datetime.strptime(
                request.form["renewal_date"], "%Y-%m-%d"
            ).date(),
            reminder_days=int(request.form["reminder_days"]),
            reminder_email=(request.form.get("reminder_email") or "").strip() or None,
            status="Active",
        )
        db.session.add(subscription)
        db.session.commit()

        response = make_response(redirect(url_for("index")))
        return attach_user_cookie(response, user_id)

    response = make_response(render_template("add_subscription.html"))
    return attach_user_cookie(response, user_id)


@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_subscription(id):
    user_id = get_anonymous_user()
    subscription = Subscription.query.filter_by(
        id=id,
        anonymous_user_id=user_id,
    ).first_or_404()

    if request.method == "POST":
        subscription.name = request.form["name"].strip()
        subscription.category = request.form["category"]
        subscription.price = float(request.form["price"])
        subscription.currency = request.form["currency"]
        subscription.billing_cycle = request.form["billing_cycle"]
        subscription.renewal_date = datetime.strptime(
            request.form["renewal_date"], "%Y-%m-%d"
        ).date()
        subscription.reminder_days = int(request.form["reminder_days"])
        subscription.reminder_email = (
            (request.form.get("reminder_email") or "").strip() or None
        )
        subscription.status = request.form["status"]
        db.session.commit()
        return redirect(url_for("index"))

    return render_template("edit_subscription.html", subscription=subscription)


@app.post("/delete/<int:id>")
def delete_subscription(id):
    user_id = get_anonymous_user()
    subscription = Subscription.query.filter_by(
        id=id,
        anonymous_user_id=user_id,
    ).first_or_404()

    # Delete reminder logs explicitly so this also works on local SQLite.
    ReminderLog.query.filter_by(subscription_id=subscription.id).delete()
    db.session.delete(subscription)
    db.session.commit()
    return redirect(url_for("index"))


# -----------------------------------------------------------------------------
# Scheduled reminder endpoint (called by Vercel Cron)
# -----------------------------------------------------------------------------

@app.get("/api/cron/reminders")
def cron_reminders():
    cron_secret = os.getenv("CRON_SECRET")
    auth_header = request.headers.get("Authorization", "")

    if not cron_secret or auth_header != f"Bearer {cron_secret}":
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    today = datetime.now(timezone.utc).date()

    subscriptions = Subscription.query.filter(
        Subscription.status == "Active",
        Subscription.reminder_email.isnot(None),
    ).all()

    normalize_renewal_dates(subscriptions, today)

    sent = 0
    skipped = 0
    failed = []

    for subscription in subscriptions:
        days_left = (subscription.renewal_date - today).days

        # Send once inside the chosen reminder window. This also recovers if a
        # previous daily cron run was missed or an email provider briefly failed.
        if not (0 <= days_left <= subscription.reminder_days):
            skipped += 1
            continue

        existing_log = ReminderLog.query.filter_by(
            subscription_id=subscription.id,
            renewal_date=subscription.renewal_date,
            reminder_days=subscription.reminder_days,
        ).first()

        if existing_log:
            skipped += 1
            continue

        try:
            resend_email_id = send_subscription_reminder(subscription, days_left)

            db.session.add(
                ReminderLog(
                    subscription_id=subscription.id,
                    renewal_date=subscription.renewal_date,
                    reminder_days=subscription.reminder_days,
                    recipient_email=subscription.reminder_email,
                    resend_email_id=resend_email_id,
                )
            )
            db.session.commit()
            sent += 1

        except Exception as exc:
            db.session.rollback()
            failed.append(
                {
                    "subscription_id": subscription.id,
                    "name": subscription.name,
                    "error": str(exc),
                }
            )

    return jsonify(
        {
            "ok": len(failed) == 0,
            "date_utc": today.isoformat(),
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


# Prototype-friendly table creation. For a larger production app, switch this
# to Alembic/Flask-Migrate migrations.
with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
