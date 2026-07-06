# Complete Google Cloud Setup Guide

## Step 1: Create a Google Cloud Project

1. Go to https://console.cloud.google.com
2. Sign in with your Google account (the same one whose emails you want to clean)
3. Click the project dropdown at the top of the page (near the search bar)
4. Click **"New Project"**
5. Enter a project name, e.g. `Gmail Bulk Cleaner`
6. Leave organization as **"No organization"** (unless you have one)
7. Click **"Create"**

## Step 2: Enable the Gmail API

1. With your new project selected, go to **"APIs & Services"** → **"Library"**
   (or use this link: https://console.cloud.google.com/apis/library)
2. Search for **"Gmail API"**
3. Click on **"Gmail API"**
4. Click **"Enable"**

## Step 3: Configure OAuth Consent Screen

1. Go to **"APIs & Services"** → **"OAuth consent screen"**
   (https://console.cloud.google.com/apis/credentials/consent)
2. Select **"External"** user type (even for personal use) and click **"Create"**
3. Fill in the required fields:
   - **App name**: `Gmail Bulk Cleaner`
   - **User support email**: your email address
   - **Developer contact information**: your email address
4. Click **"Save and Continue"**
5. **Scopes** page: Click **"Add or Remove Scopes"**
6. In the filter box, paste: `https://www.googleapis.com/auth/gmail.modify`
7. Check the box next to **".../auth/gmail.modify"** and click **"Update"**
8. Click **"Save and Continue"**
9. **Test users** page: Click **"Add Users"**, enter your email address, click **"Add"**, then **"Save and Continue"**
10. **Summary** page: Click **"Back to Dashboard"**

## Step 4: Create OAuth Credentials

1. Go to **"APIs & Services"** → **"Credentials"**
   (https://console.cloud.google.com/apis/credentials)
2. Click **"+ Create Credentials"** → **"OAuth client ID"**
3. For **Application type**, select **"Web application"**
4. **Name**: `Gmail Bulk Cleaner Web`
5. Under **"Authorized redirect URIs"**, click **"+ Add URI"** and add:
   - For local testing: `http://localhost:5000/oauth2callback`
   - For production (once deployed): `https://yourdomain.com/oauth2callback`
   (Replace `yourdomain.com` with your actual domain or deployment URL)
6. Click **"Create"**
7. A popup will show your **Client ID** and **Client Secret**
8. Click **"Download JSON"** to save the credentials file

## Step 5: Configure the App

1. In the `gmail-cleaner-web` folder, copy `.env.example` to `.env`:
   ```
   copy .env.example .env
   ```
2. Open `.env` and fill in the values from Step 4:
   ```
   FLASK_SECRET_KEY=your-random-secret-key
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your-client-secret
   OAUTH_REDIRECT_URI=http://localhost:5000/oauth2callback
   ```

   To generate a random `FLASK_SECRET_KEY`, run:
   ```
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Run locally:
   ```
   python app.py
   ```

5. Open http://localhost:5000 in your browser

## Step 6: Deploy to Production

### Option A: Render (free tier)
1. Create a free account at https://render.com
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub repo (or upload the code)
4. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Add the environment variables from `.env` in the Render dashboard
6. Update `OAUTH_REDIRECT_URI` in `.env` to `https://your-app.onrender.com/oauth2callback`
7. Also add this same URI in Google Cloud Console (Step 4.5)
8. Deploy

### Option B: Railway (free tier)
1. Create a free account at https://railway.app
2. Click **"New Project"** → **"Deploy from GitHub"**
3. Set the start command to `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Add environment variables
5. Deploy

### Option C: Your own VPS
```
pip install gunicorn
gunicorn app:app --bind 0.0.0.0:8000 --workers 2
```
Use nginx as a reverse proxy and certbot for HTTPS.

## Step 7: Link from WordPress

1. Go to your WordPress admin dashboard
2. Navigate to **Appearance** → **Menus**
3. Click **"Custom Links"**
4. **URL**: Enter your deployed app URL (e.g. `https://your-app.onrender.com`)
5. **Link Text**: Enter `Gmail Bulk Cleaner` (or whatever you want)
6. Click **"Add to Menu"**
7. Position it wherever you like
8. Click **"Save Menu"**

## Important Notes

- The first time you sign in, Google will show a warning since the app is unverified.
  Click **"Advanced"** → **"Go to Gmail Bulk Cleaner (unsafe)"** — this is safe, it just means Google hasn't reviewed the app.
- The app only requests `gmail.modify` scope, which allows reading, composing, and deleting emails — but NOT sending emails or changing account settings.
- You can revoke access anytime at https://myaccount.google.com/permissions
