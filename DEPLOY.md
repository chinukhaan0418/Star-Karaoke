# Deploy: GitHub + Netlify (website) + backend host (server.py)

See the chat for the same steps. Summary:
1. Push this folder to a PRIVATE GitHub repo (stars.db is already excluded).
2. Create a backend service from the repo using the Dockerfile. Add a persistent volume
   mounted at /data. Set env: ADMIN_PASSWORD (strong), ADMIN_USERNAME (optional).
3. Copy the backend's public URL into netlify.toml (replace YOUR-BACKEND-URL), commit, push.
4. On Netlify: Add new site > Import from GitHub > pick the repo. Publish dir "public" is read from netlify.toml.
5. Test the site, then log in at /admin.html.
