This is how the .env should look, fill in your details

# --- Veracross (find these in your school's portal URL) ---
VERACROSS_LOGIN_URL=
VERACROSS_ASSIGNMENTS_URL=
VERACROSS_USERNAME=
VERACROSS_PASSWORD=
VERACROSS_HEADLESS=auto
# --- Ollama ---
OLLAMA_URL=http://localhost:11434
# Leave blank to auto-use the first installed model
OLLAMA_MODEL=
 
# Extra folders the AI may READ (comma separated), e.g. ~/Documents,~/Projects
# It can only WRITE inside AgentOS/brain. Do not list the AgentOS folder itself.
AGENT_READ_ROOTS=~/Documents,~/Projects
