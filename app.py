import google.generativeai as genai
import os, json, re, time, random
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
 
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
 
# ✅ NEW: MongoDB cache collections (persistent forever)
remedy_cache_col  = db["remedy_cache"]
recipe_cache_col  = db["recipe_cache"]
 
# ✅ Sends Content-Language header on every response
# Chrome sees the page is already in English and does NOT show the translate bar
@app.after_request
def add_header(response):
    response.headers['Content-Language'] = 'en'
    return response
 
 
# ============================================================
# ✅ GEMINI HELPER — retry up to 4 times with backoff
# ============================================================
def call_gemini_with_retry(prompt, retries=4):
    """
    Calls Gemini API with exponential backoff on 429 rate-limit errors.
    Tries gemini-1.5-flash first, falls back to gemini-pro if needed.
    """
    models_to_try = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
 
    for model_name in models_to_try:
        for attempt in range(retries):
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                print(f"✅ Success with model: {model_name} on attempt {attempt + 1}")
                return response.text
            except Exception as e:
                error_str = str(e)
                if "429" in error_str:
                    # Exponential backoff: 2s, 4s, 8s, 16s
                    wait_time = (2 ** (attempt + 1)) + random.uniform(0, 1)
                    print(f"⚠️ Rate limit on {model_name}, attempt {attempt + 1}. Waiting {wait_time:.1f}s...")
                    time.sleep(wait_time)
                elif "404" in error_str or "not found" in error_str.lower():
                    # Model not available, try next model
                    print(f"❌ Model {model_name} not found, trying next...")
                    break
                else:
                    print(f"❌ Error on {model_name}: {e}")
                    raise e
 
    raise Exception("429: All models rate limited. Please wait a few minutes.")
 
 
# ============================================================
# ✅ MONGODB CACHE HELPERS
# ============================================================
def get_cached_remedy(query):
    """Check MongoDB for a cached remedy result."""
    result = remedy_cache_col.find_one({"query": query})
    if result:
        print(f"✅ Cache HIT for remedy: {query}")
        return result["data"]
    return None
 
def save_cached_remedy(query, data):
    """Save remedy result to MongoDB cache."""
    remedy_cache_col.update_one(
        {"query": query},
        {"$set": {"query": query, "data": data}},
        upsert=True
    )
    print(f"✅ Cached remedy saved for: {query}")
 
def get_cached_recipe(query):
    """Check MongoDB for a cached recipe result."""
    result = recipe_cache_col.find_one({"query": query})
    if result:
        print(f"✅ Cache HIT for recipe: {query}")
        return result["data"]
    return None
 
def save_cached_recipe(query, data):
    """Save recipe result to MongoDB cache."""
    recipe_cache_col.update_one(
        {"query": query},
        {"$set": {"query": query, "data": data}},
        upsert=True
    )
    print(f"✅ Cached recipe saved for: {query}")
 
 
# ============================================================
# ✅ PARSE JSON HELPER
# ============================================================
def parse_json_response(text):
    """Safely extract JSON array from Gemini response text."""
    text = text.strip().replace("```json", "").replace("```", "")
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError as e:
            print(f"❌ JSON parse error: {e}")
            return None
    return None
 
 
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
        model = genai.GenerativeModel("gemini-1.5-flash")
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
 
 
# ============================================================
# ✅ REMEDIES — with MongoDB cache + retry logic
# ============================================================
@app.route("/remedies", methods=["GET", "POST"])
def remedies():
    query = request.form.get("query", "").lower().strip()
    results = []
 
    if query:
        # ✅ STEP 1: Check MongoDB cache first (never hits API if cached)
        cached = get_cached_remedy(query)
        if cached:
            return render_template("remedies.html", query=query, results=cached)
 
        # ✅ STEP 2: Not cached — call Gemini with retry + backoff
        try:
            prompt = f"""
Give exactly 4 Ayurvedic remedies for "{query}".
 
STRICT RULES:
- Return ONLY a valid JSON array, no extra text, no markdown
 
FORMAT:
[
  {{
    "disease": "...",
    "ingredients": ["...", "..."],
    "method": ["Step 1: ...", "Step 2: ..."],
    "frequency": "...",
    "dosha": "...",
    "category": "..."
  }}
]
"""
            text = call_gemini_with_retry(prompt)
            results = parse_json_response(text)
 
            if results:
                # ✅ STEP 3: Save to MongoDB so next search is instant
                save_cached_remedy(query, results)
            else:
                flash("⚠️ Could not parse response. Please try again.")
 
        except Exception as e:
            print("REMEDIES ERROR:", e)
            if "429" in str(e):
                flash("⚠️ API is busy right now. Please wait 1-2 minutes and try again.")
            elif "403" in str(e):
                flash("⚠️ API access issue. Please try later.")
            else:
                flash("⚠️ Something went wrong. Please try again.")
 
    return render_template("remedies.html", query=query, results=results or [])
 
 
# ----------- DIET -----------
@app.route("/diet")
def diet():
    return render_template("diet.html")
 
 
# ============================================================
# ✅ RECIPES — with MongoDB cache + retry logic
# ============================================================
@app.route("/recipes", methods=["GET", "POST"])
def recipes():
    query = request.form.get("query", "").strip()
    results = []
 
    if query:
        # ✅ STEP 1: Check MongoDB cache first
        cached = get_cached_recipe(query.lower())
        if cached:
            return render_template("recipes.html", query=query, results=cached)
 
        # ✅ STEP 2: Not cached — call Gemini with retry + backoff
        try:
            prompt = f"""
Give exactly 3 Ayurvedic recipes for "{query}".
 
STRICT RULES:
- Return ONLY a valid JSON array, no extra text, no markdown
 
FORMAT:
[
  {{
    "name": "...",
    "ingredients": ["...", "..."],
    "process": ["Step 1: ...", "Step 2: ..."],
    "benefits": ["...", "..."],
    "dosha": "..."
  }}
]
"""
            text = call_gemini_with_retry(prompt)
            results = parse_json_response(text)
 
            if results:
                # ✅ STEP 3: Save to MongoDB so next search is instant
                save_cached_recipe(query.lower(), results)
            else:
                flash("⚠️ Could not parse response. Please try again.")
 
        except Exception as e:
            print("RECIPES ERROR:", e)
            if "429" in str(e):
                flash("⚠️ API is busy right now. Please wait 1-2 minutes and try again.")
            elif "403" in str(e):
                flash("⚠️ API access issue. Please try later.")
            else:
                flash("⚠️ Something went wrong. Please try again.")
 
    return render_template("recipes.html", query=query, results=results or [])
 
 
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
        "yoga":       list(yoga_col.find({}, {"_id": 0})),
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