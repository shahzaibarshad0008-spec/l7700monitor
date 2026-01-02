Method 1: Using Node.js and npm (Recommended)
This is the standard and most flexible way, offering full customization and an optimized production build. 
Set up your Python project: Create your Flask or Django project with the necessary folder structure (e.g., templates for HTML, static for CSS).
Initialize npm: In your project root directory, open a terminal and run the following command to create a package.json file:
bash
npm init -y
Install Tailwind CSS: Install tailwindcss via npm:
bash
npm install -D tailwindcss
Create a Tailwind config file: Generate the configuration file (tailwind.config.js):
bash
npx tailwindcss init
Configure template paths: In the generated tailwind.config.js file, update the content section to tell Tailwind where your HTML files are located:
javascript
module.exports = {
  content: ["./templates/**/*.html", "./**/*.py"], // Example paths for typical Python projects
  // ...
}
Create an input CSS file: Create an input CSS file (e.g., static/css/input.css) and add the Tailwind directives:
css
@tailwind base;
@tailwind components;
@tailwind utilities;
Run the build process: In one terminal, run the build command to generate the output CSS file and watch for changes during development:
bash
npx tailwindcss -i ./static/css/input.css -o ./static/css/output.css --watch
Link the output CSS: In your HTML templates, link to the generated output.css file:
html
<link href="{{ url_for('static', filename='css/output.css') }}" rel="stylesheet"> <!-- For Flask -->
<!-- or for Django -->
{% load static %}
<link href="{% static 'css/output.css' %}" rel="stylesheet">
Run your Python app: In a separate terminal, run your Python development server (e.g., python app.py for Flask or python manage.py runserver for Django). 

cat > README.md <<'EOF'
# L7700 Hospital Monitor (FastAPI + RTSP + Real-time Dashboard)

A real-time hospital monitoring system that:
- receives **L7700 device events over UDP**
- decodes and stores them in **MySQL**
- updates the UI in real time via **WebSocket**
- streams **RTSP cameras** to the browser using **MJPEG**

---

## ✨ Key Features

- ✅ UDP listener for L7700 packets
- ✅ Packet decoding + event storage in MySQL
- ✅ Real-time updates via WebSocket (`/ws`)
- ✅ Camera streaming (RTSP → MJPEG)
- ✅ Calls page shows active calls + optional camera feed per room
- ✅ Config APIs for floors/wards/rooms/beds/colors/cameras

---

## 🧱 Tech Stack

- **Backend:** FastAPI + Uvicorn
- **Database:** MySQL (socket based)
- **ORM:** SQLAlchemy
- **Camera:** OpenCV (RTSP capture) + MJPEG streaming
- **Frontend:** HTML + Tailwind CSS (static)

---

## 📁 Project Structure (important files)

- `server.py` — FastAPI server, APIs, WebSocket, UDP listener, MJPEG routes
- `camera_stream.py` — RTSP camera manager (RealCameraStream / Simulator)
- `decoder.py` — L7700 UDP packet decoding logic
- `models.py` — SQLAlchemy models + database initialization
- `config.py` — ports, DB credentials, server host, UDP settings
- `templates/dashboard.html` — dashboard UI
- `templates/calls.html` — real-time calls UI
- `static/` — CSS/JS assets

---

## ✅ Requirements

### System (Ubuntu recommended)
- Python 3.9+ (recommended 3.10+)
- MySQL (local, socket accessible)
- FFmpeg (recommended for RTSP stability)
- OpenCV dependencies (usually included with `opencv-python`)

Install FFmpeg:
```bash
sudo apt update
sudo apt install -y ffmpeg
