import google.generativeai as genai
import os, json, re
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
import time

# simple cache (memory)
cache = {}

# ✅ Load env FIRST
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not set")

genai.configure(api_key=API_KEY)

app = Flask(__name__)
app.secret_key = "your_secret_key"

# ✅ MongoDB ONCE only
mongo_uri = os.getenv("MONGO_URI")

if not mongo_uri:
    raise ValueError("❌ MONGO_URI not set")

client = MongoClient(mongo_uri)
db = client["wellnessDB"]

users_collection    = db["users"]
profiles_collection = db["profiles"]
yoga_col            = db["yoga"]
meditation_col      = db["meditation"]
routine_col         = db["routine"]
user_routines       = db["user_routines"]
dosh_test_col       = db["dosh_test"]

# ✅ THIS IS THE KEY FIX FOR CHROME BAR
# Sends Content-Language header on every response
# Chrome sees the page is already in English and does NOT show the translate bar
@app.after_request
def add_header(response):
    response.headers['Content-Language'] = 'en'
    return response

# ✅ Helper — finds first working Gemini model
def get_gemini_model():
    preferred = [
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-pro",
        "gemini-1.0-pro",
    ]
    try:
        available = [
            m.name.replace("models/", "")
            for m in genai.list_models()
            if "generateContent" in m.supported_generation_methods
        ]
        print("✅ Available models:", available)
        for model_name in preferred:
            if model_name in available:
                print(f"✅ Using model: {model_name}")
                return genai.GenerativeModel(model_name)
        if available:
            print(f"⚠️ Fallback model: {available[0]}")
            return genai.GenerativeModel(available[0])
    except Exception as e:
        print("❌ Could not list models:", e)
    return genai.GenerativeModel("gemini-pro")

# ---------------- ROUTES ----------------

@app.route("/")
def landing():
    return render_template("landingpage.html")

@app.route("/list-models")
def list_models():
    try:
        models = genai.list_models()
        available = []
        for m in models:
            if "generateContent" in m.supported_generation_methods:
                available.append(m.name)
        return jsonify({"available_models": available})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/test-gemini")
def test_gemini():
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content("Say hello in one word")
        return jsonify({"status": "✅ Working", "response": response.text})
    except Exception as e:
        return jsonify({"status": "❌ Failed", "error": str(e)})

# ----------- SIGN UP -----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username").strip()
        email    = request.form.get("email").strip()
        password = request.form.get("password").strip()

        if users_collection.find_one({"email": email}):
            flash("User already exists!")
            return redirect(url_for("signin"))

        hashed_pw = generate_password_hash(password)
        user_id = users_collection.insert_one({
            "username": username,
            "email": email,
            "password": hashed_pw
        }).inserted_id

        session["user_id"]  = str(user_id)
        session["username"] = username
        return redirect(url_for("dashboard"))

    return render_template("signup.html")

# ----------- SIGNIN -----------
@app.route("/signin", methods=["GET", "POST"])
def signin():
    if request.method == "POST":
        email    = request.form.get("email")
        password = request.form.get("password")

        user = users_collection.find_one({"email": email})
        if user and check_password_hash(user["password"], password):
            session["user_id"]  = str(user["_id"])
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))

        flash("Invalid credentials")
    return render_template("signin.html")

# ----------- FORGOT PASSWORD -----------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email        = request.form.get("email").strip()
        new_password = request.form.get("password").strip()

        user = users_collection.find_one({"email": email})
        if not user:
            flash("User not found")
            return redirect(url_for("forgot_password"))

        users_collection.update_one(
            {"email": email},
            {"$set": {"password": generate_password_hash(new_password)}}
        )
        flash("Password updated successfully! Please login.")
        return redirect(url_for("signin"))

    return render_template("forgot_password.html")

# ----------- CONTEXT PROCESSOR -----------
@app.context_processor
def inject_user():
    if "user_id" in session:
        try:
            user = users_collection.find_one({"_id": ObjectId(session["user_id"])})
            return dict(user=user)
        except:
            return dict(user=None)
    return dict(user=None)

# ----------- DASHBOARD -----------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("signin"))
    user = users_collection.find_one({"_id": ObjectId(session["user_id"])})
    return render_template("dashboard.html", user=user)

# ----------- LOGOUT -----------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))

# ----------- PROFILE -----------
@app.route("/myprofile", methods=["GET", "POST"])
def myprofile():
    user_id = ObjectId(session["user_id"])
    user    = users_collection.find_one({"_id": user_id})
    profile = profiles_collection.find_one({"user_id": user_id})

    if request.method == "POST":
        data = request.form.to_dict()
        data["user_id"] = user_id
        if profile:
            profiles_collection.update_one({"_id": profile["_id"]}, {"$set": data})
        else:
            profiles_collection.insert_one(data)
        return redirect(url_for("dashboard"))

    return render_template("myprofile.html", profile=profile, user=user)

# ----------- SAVE / GET ROUTINE -----------
@app.route("/api/save-routine", methods=["POST"])
def save_routine():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    user_routines.update_one(
        {"user_id": session["user_id"]},
        {"$set": {"routine": data}},
        upsert=True
    )
    return jsonify({"message": "Saved"})

@app.route("/api/get-user-routine")
def get_user_routine():
    routine = user_routines.find_one({"user_id": session.get("user_id")})
    return jsonify(routine["routine"] if routine else [])

@app.route("/api/save-dosha", methods=["POST"])
def save_dosha():
    users_collection.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$set": {"dosha": request.json.get("dosha")}}
    )
    return jsonify({"message": "Saved"})

# ----------- REMEDIES -----------
@app.route("/remedies", methods=["GET", "POST"])
def remedies():
    query = request.form.get("query", "").lower().strip()
    results = []

    if query:

        # ✅ CACHE CHECK
        if query in cache:
            return render_template("remedies.html", query=query, results=cache[query])

        try:
            time.sleep(1)  # ✅ prevent rate burst

            prompt = f"""
Give exactly 4 Ayurvedic remedies for "{query}".

STRICT RULES:
- Return ONLY JSON array

FORMAT:
[
  {{
    "disease": "...",
    "ingredients": ["..."],
    "method": ["..."],
    "frequency": "...",
    "dosha": "...",
    "category": "..."
  }}
]
"""

            # ✅ FIXED MODEL
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)

            text = response.text.strip().replace("```json", "").replace("```", "")

            match = re.search(r"\[.*\]", text, re.DOTALL)

            if match:
                results = json.loads(match.group())
                cache[query] = results   # ✅ store in cache
            else:
                flash("⚠️ Try again")

        except Exception as e:
            print("ERROR:", e)

            if "429" in str(e):
                flash("⚠️ Too many requests. Wait 1 minute.")
            elif "403" in str(e):
                flash("⚠️ API issue. Try later.")
            else:
                flash("⚠️ Something went wrong.")

    return render_template("remedies.html", query=query, results=results)

# ----------- DIET -----------
@app.route("/diet")
def diet():
    return render_template("diet.html")

# ----------- RECIPES -----------
@app.route("/recipes", methods=["GET", "POST"])
def recipes():
    query = request.form.get("query", "").strip()
    results = []

    if query:

        # ✅ CACHE CHECK
        if query in cache:
            return render_template("recipes.html", query=query, results=cache[query])

        try:
            time.sleep(1)

            prompt = f"""
Give exactly 3 Ayurvedic recipes for "{query}".

STRICT RULES:
- Return ONLY JSON

FORMAT:
[
  {{
    "name": "...",
    "ingredients": ["..."],
    "process": ["..."],
    "benefits": ["..."],
    "dosha": "..."
  }}
]
"""

            # ✅ FIXED MODEL
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)

            text = response.text.strip().replace("```json", "").replace("```", "")

            match = re.search(r"\[.*\]", text, re.DOTALL)

            if match:
                results = json.loads(match.group())
                cache[query] = results
            else:
                flash("⚠️ Try again")

        except Exception as e:
            print("ERROR:", e)

            if "429" in str(e):
                flash("⚠️ Too many requests. Wait 1 minute.")
            elif "403" in str(e):
                flash("⚠️ API issue.")
            else:
                flash("⚠️ Unable to fetch recipes.")

    return render_template("recipes.html", query=query, results=results)
# ----------- OTHER PAGES -----------
@app.route("/dosh")
def dosh():
    return render_template("dosh.html")

@app.route("/api/questions")
def questions():
    return jsonify(list(dosh_test_col.find({}, {"_id": 0})))

@app.route("/api/data")
def get_data():
    return jsonify({
        "yoga":      list(yoga_col.find({}, {"_id": 0})),
        "meditation": list(meditation_col.find({}, {"_id": 0}))
    })

@app.route("/routine")
def routine():
    return render_template("routine.html")

@app.route("/api/routine")
def get_routine():
    return jsonify(list(routine_col.find({}, {"_id": 0})))

@app.route("/panchakarma")
def panchakarma():
    return render_template("panchkarma.html")

@app.route("/yoga")
def yoga():
    return render_template("yoga.html")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)