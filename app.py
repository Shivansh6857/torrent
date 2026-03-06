import streamlit as st
import pandas as pd
import psycopg2
import hashlib
import json
import base64
import math
import io
import zipfile
import uuid
from datetime import datetime, timedelta

# ==========================================
# 0. FLASH MESSAGES (Bug Fix for disappearing toasts)
# ==========================================
def set_flash(msg, msg_type="success"):
    st.session_state.flash_msg = {"msg": msg, "type": msg_type}

def display_flash():
    if 'flash_msg' in st.session_state:
        msg = st.session_state.flash_msg['msg']
        m_type = st.session_state.flash_msg['type']
        if m_type == "success": st.success(msg)
        elif m_type == "error": st.error(msg)
        elif m_type == "warning": st.warning(msg)
        elif m_type == "toast": st.toast(msg, icon="✅")
        del st.session_state.flash_msg

# ==========================================
# 1. DATABASE CONFIGURATION & AUTO-MIGRATION
# ==========================================
class SupabaseConnection:
    def __init__(self, dsn):
        self.conn = psycopg2.connect(dsn)
    def execute(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur
    def commit(self):
        self.conn.commit()
    def cursor(self):
        return self.conn.cursor()
    def close(self):
        self.conn.close()

def get_connection():
    # URL-encoded the '@' in the password to '%40' to prevent parsing errors
    return SupabaseConnection("postgresql://postgres:Shivansh%402023@db.omkxqeexstupwaoejzdx.supabase.co:5432/postgres")

def init_db():
    conn = get_connection()
    c = conn.cursor()
    
    # Core SaaS Tables
    c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, email TEXT UNIQUE, first_name TEXT, last_name TEXT, password TEXT, security_question TEXT, security_answer TEXT, is_active INTEGER DEFAULT 1, session_token TEXT, last_login_time TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, name TEXT, owner TEXT, is_active INTEGER DEFAULT 1)''')
    
    # Workspace Members
    c.execute('''CREATE TABLE IF NOT EXISTS workspace_members (username TEXT, workspace_id TEXT, role_name TEXT, assigned_locations TEXT DEFAULT '[]', invite_status TEXT DEFAULT 'accepted', PRIMARY KEY(username, workspace_id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS roles (role_name TEXT PRIMARY KEY, permissions TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS issue_categories (id SERIAL PRIMARY KEY, name TEXT, workspace_id TEXT, is_active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')

    # Data Tables
    c.execute('''CREATE TABLE IF NOT EXISTS locations (id SERIAL PRIMARY KEY, name TEXT, workspace_id TEXT, is_active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS inventory (id SERIAL PRIMARY KEY, item_code TEXT, item_name TEXT, location TEXT, workspace_id TEXT, book_qty REAL, unit_price REAL, total_counted REAL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS counts (id SERIAL PRIMARY KEY, item_id INTEGER, "user" TEXT, workspace_id TEXT, added_qty REAL, timestamp TIMESTAMP, comment TEXT, image_data TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS issues (id SERIAL PRIMARY KEY, item_id INTEGER, unlisted_item TEXT, location TEXT, workspace_id TEXT, "user" TEXT, category TEXT, comment TEXT, image_data TEXT, timestamp TIMESTAMP)''')

    # --- Migrations for existing V2 databases ---
    c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
    usr_cols = [info[0] for info in c.fetchall()]
    if 'email' not in usr_cols: c.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if 'first_name' not in usr_cols: c.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
    if 'last_name' not in usr_cols: c.execute("ALTER TABLE users ADD COLUMN last_name TEXT")
    if 'security_question' not in usr_cols: c.execute("ALTER TABLE users ADD COLUMN security_question TEXT")
    if 'security_answer' not in usr_cols: c.execute("ALTER TABLE users ADD COLUMN security_answer TEXT")

    c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'workspace_members'")
    wm_cols = [info[0] for info in c.fetchall()]
    if 'invite_status' not in wm_cols: c.execute("ALTER TABLE workspace_members ADD COLUMN invite_status TEXT DEFAULT 'accepted'")

    # Seed Default Roles
    all_perms = json.dumps(["Counting Portal", "Dashboard & Export", "Location Import", "Masters & Settings", "Issue Reports", "Standalone Issue Report", "Data Export & Reports", "Manage Clients", "Manage Locations", "Manage Roles", "Manage Users", "Manage Categories", "Manage System Settings"])
    c.execute("INSERT INTO roles (role_name, permissions) VALUES (%s, %s) ON CONFLICT (role_name) DO NOTHING", (all_perms,))
        
    conn.commit()
    return conn

db_conn = init_db()

# --- HELPER FUNCTIONS ---
def get_setting(workspace_id, key_name, default_value):
    row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f"{key_name}_{workspace_id}",)).fetchone()
    return row[0] if row else default_value

def safe_float(val):
    try: return float(pd.to_numeric(val, errors='coerce')) or 0.0
    except: return 0.0

def get_current_time(workspace_id):
    if not workspace_id:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        override_active = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'override_active_{workspace_id}',)).fetchone()
        if override_active and override_active[0] == '1':
            base_str = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'override_base_time_{workspace_id}',)).fetchone()[0]
            start_str = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'override_real_start_{workspace_id}',)).fetchone()[0]
            base_dt = datetime.strptime(base_str, "%Y-%m-%d %H:%M:%S")
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            elapsed = datetime.now() - start_dt
            return (base_dt + elapsed).strftime("%Y-%m-%d %H:%M:%S")
    except: pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_display_time(workspace_id):
    dt_obj = datetime.strptime(get_current_time(workspace_id), "%Y-%m-%d %H:%M:%S")
    return dt_obj.strftime('%A, %d %B %Y | %I:%M:%S %p')

def process_image(uploaded_file):
    if uploaded_file is not None:
        return base64.b64encode(uploaded_file.read()).decode('utf-8')
    return None

def get_allowed_locations(workspace_id):
    all_locs = pd.read_sql("SELECT name FROM locations WHERE is_active=1 AND workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist()
    en_loc_assign_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'enable_location_assignment_{workspace_id}',)).fetchone()
    en_loc_assign = en_loc_assign_row[0] if en_loc_assign_row else '0'
    
    if en_loc_assign == '1' and st.session_state.get('role') != 'Super Admin':
        u_assigned_str = db_conn.execute("SELECT assigned_locations FROM workspace_members WHERE username=%s AND workspace_id=%s", (st.session_state.username, workspace_id)).fetchone()
        try: u_assigned = json.loads(u_assigned_str[0]) if u_assigned_str else []
        except: u_assigned = []
        return [loc for loc in all_locs if loc in u_assigned]
    return all_locs

def get_user_display(username, workspace_id):
    """Fetches user representation based on workspace preference."""
    pref_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'display_pref_{workspace_id}',)).fetchone()
    pref = pref_row[0] if pref_row else "Username"
    
    u_data = db_conn.execute("SELECT email, first_name, last_name FROM users WHERE username=%s", (username,)).fetchone()
    if not u_data: return username
    
    email, fn, ln = u_data
    if pref == "Email": 
        return email if email else username
    elif pref == "Display Name": 
        full_name = f"{fn or ''} {ln or ''}".strip()
        return full_name if full_name else username
    return username

# ==========================================
# 2. USER PROFILE & INVITES PAGE
# ==========================================
def user_profile_page():
    st.header("👤 My Profile")
    st.write("Manage your global account settings and workspace invites.")
    
    # --- PENDING INVITES ---
    pending_invites = db_conn.execute("""
        SELECT w.id, w.name, wm.role_name, w.owner 
        FROM workspace_members wm 
        JOIN workspaces w ON wm.workspace_id = w.id 
        WHERE wm.username=%s AND wm.invite_status='pending' AND w.is_active=1
    """, (st.session_state.username,)).fetchall()
    
    if pending_invites:
        st.warning("🔔 You have pending workspace invitations!")
        for inv in pending_invites:
            with st.container(border=True):
                st.write(f"**{inv[3]}** has invited you to join the workspace **'{inv[1]}'** as a **{inv[2]}**.")
                i_c1, i_c2 = st.columns(2)
                
                def cb_acc(w_id, w_name):
                    db_conn.execute("UPDATE workspace_members SET invite_status='accepted' WHERE username=%s AND workspace_id=%s", (st.session_state.username, w_id))
                    db_conn.commit()
                    set_flash(f"Welcome to {w_name}!")
                def cb_dec(w_id):
                    db_conn.execute("DELETE FROM workspace_members WHERE username=%s AND workspace_id=%s", (st.session_state.username, w_id))
                    db_conn.commit()
                    set_flash("Invite declined.")

                i_c1.button("✅ Accept Invite", key=f"acc_{inv[0]}", type="primary", on_click=cb_acc, args=(inv[0], inv[1]))
                i_c2.button("❌ Decline", key=f"dec_{inv[0]}", on_click=cb_dec, args=(inv[0],))

    # --- PROFILE SETTINGS ---
    u_details = db_conn.execute("SELECT email, first_name, last_name FROM users WHERE username=%s", (st.session_state.username,)).fetchone()
    curr_email, curr_fn, curr_ln = u_details if u_details else ("", "", "")

    with st.container(border=True):
        st.subheader("📝 Update Profile Details")
        
        def cb_upd_prof():
            new_fn = st.session_state.get("upd_fn", "")
            new_ln = st.session_state.get("upd_ln", "")
            new_email = st.session_state.get("upd_email", "").strip().lower()
            
            if new_email and new_email != (curr_email.lower() if curr_email else ""):
                if db_conn.execute("SELECT COUNT(*) FROM users WHERE email=%s", (new_email,)).fetchone()[0] > 0:
                    set_flash("Email is already registered to another account.", "error")
                    return
                    
            db_conn.execute("UPDATE users SET first_name=%s, last_name=%s, email=%s WHERE username=%s", (new_fn.strip(), new_ln.strip(), new_email, st.session_state.username))
            db_conn.commit()
            set_flash("Profile updated successfully!")

        st.text_input("First Name", value=curr_fn, key="upd_fn")
        st.text_input("Last Name", value=curr_ln, key="upd_ln")
        st.text_input("Email Address", value=curr_email, key="upd_email")
        st.button("Save Profile", type="primary", on_click=cb_upd_prof)

    with st.container(border=True):
        st.subheader("🏢 Create a New Workspace")
        st.caption("You will automatically be the Super Admin of this new isolated space.")
        
        def cb_create_ws():
            new_ws_name = st.session_state.get("new_ws_name", "").strip()
            if new_ws_name:
                new_id = uuid.uuid4().hex
                db_conn.execute("INSERT INTO workspaces (id, name, owner, is_active) VALUES (%s, %s, %s, 1)", (new_id, new_ws_name, st.session_state.username))
                db_conn.execute("INSERT INTO workspace_members (username, workspace_id, role_name, invite_status) VALUES (%s, %s, 'Super Admin', 'accepted')", (st.session_state.username, new_id))
                for cat in ["Expired Stock", "Batch Error", "Damaged Item", "Other"]:
                    db_conn.execute("INSERT INTO issue_categories (name, workspace_id, is_active) VALUES (%s, %s, 1)", (cat, new_id))
                db_conn.commit()
                st.session_state["new_ws_name"] = ""
                set_flash(f"Workspace '{new_ws_name}' created successfully!")

        with st.form("profile_new_workspace", clear_on_submit=True):
            st.text_input("Workspace Name", key="new_ws_name")
            st.form_submit_button("Create Workspace", on_click=cb_create_ws)

    with st.container(border=True):
        st.subheader("🔑 Change Password")
        
        def cb_change_password():
            old_p = st.session_state.get("cp_old", "")
            new_p = st.session_state.get("cp_new", "")
            confirm_p = st.session_state.get("cp_conf", "")
            if not new_p or not confirm_p: set_flash("Please enter a new password.", "error")
            elif new_p != confirm_p: set_flash("New passwords do not match.", "error")
            else:
                hp_old = hashlib.sha256(old_p.encode()).hexdigest()
                res = db_conn.execute("SELECT password FROM users WHERE username=%s", (st.session_state.username,)).fetchone()
                if res and res[0] == hp_old:
                    hp_new = hashlib.sha256(new_p.encode()).hexdigest()
                    db_conn.execute("UPDATE users SET password=%s WHERE username=%s", (hp_new, st.session_state.username))
                    db_conn.commit()
                    st.session_state["cp_old"] = ""
                    st.session_state["cp_new"] = ""
                    st.session_state["cp_conf"] = ""
                    set_flash("Password updated successfully!")
                else: set_flash("Incorrect current password.", "error")

        with st.form("change_password_form", clear_on_submit=True):
            st.text_input("Current Password", type="password", key="cp_old")
            st.text_input("New Password", type="password", key="cp_new")
            st.text_input("Confirm New Password", type="password", key="cp_conf")
            st.form_submit_button("Update Password", type="primary", on_click=cb_change_password)

# ==========================================
# 3. ADMIN: MASTERS & SETTINGS
# ==========================================
def manage_masters_page(workspace_id, workspace_name):
    st.header(f"⚙️ Masters & Settings: {workspace_name}")
    st.caption(f"🕒 {get_display_time(workspace_id)}")
    
    perms = st.session_state.permissions
    is_super = (st.session_state.role == 'Super Admin')
    has_legacy = "Masters & Settings" in perms
    
    can_loc = is_super or has_legacy or "Manage Locations" in perms
    can_role = is_super or has_legacy or "Manage Roles" in perms
    can_user = is_super or has_legacy or "Manage Users" in perms
    can_cat = is_super or has_legacy or "Manage Categories" in perms
    can_sys = is_super or has_legacy or "Manage System Settings" in perms

    t_ws, t_loc, t_role, t_user, t_iss, t_sys = st.tabs(["🏢 Workspaces", "📍 Locations", "🛡️ Roles", "👥 Users & Invites", "⚠️ Categories", "⚙️ Settings"])

    # --- WORKSPACES ---
    with t_ws:
        st.write("### Manage Current Workspace")
        ws_owner_data = db_conn.execute("SELECT owner, is_active FROM workspaces WHERE id=%s", (workspace_id,)).fetchone()
        
        if ws_owner_data and st.session_state.username == ws_owner_data[0]:
            ws_active = ws_owner_data[1]
            
            def cb_rename_ws():
                r_ws = st.session_state.get("r_ws_name")
                if r_ws and r_ws != workspace_name:
                    db_conn.execute("UPDATE workspaces SET name=%s WHERE id=%s", (r_ws.strip(), workspace_id))
                    db_conn.commit()
                    set_flash("Workspace renamed.")
                    
            st.text_input("Rename Current Workspace", value=workspace_name, key="r_ws_name")
            st.button("Update Name", on_click=cb_rename_ws)
            
            st.divider()
            st.write("### ⚠️ Danger Zone")
            has_locs = db_conn.execute("SELECT COUNT(*) FROM locations WHERE workspace_id=%s", (workspace_id,)).fetchone()[0] > 0
            has_inv = db_conn.execute("SELECT COUNT(*) FROM inventory WHERE workspace_id=%s", (workspace_id,)).fetchone()[0] > 0
            
            conf_ws = st.checkbox("I confirm I want to execute this action.", key=f"conf_ws_{workspace_id}")
            
            if has_locs or has_inv:
                btn_label = "🔴 Deactivate Workspace" if ws_active == 1 else "🟢 Reactivate Workspace"
                st.caption("Because this workspace contains active locations or inventory, it can only be deactivated, not permanently deleted.")
                
                def cb_act_ws(curr_state):
                    db_conn.execute("UPDATE workspaces SET is_active=%s WHERE id=%s", (0 if curr_state == 1 else 1, workspace_id))
                    db_conn.commit()
                    
                st.button(btn_label, disabled=not conf_ws, on_click=cb_act_ws, args=(ws_active,))
            else:
                st.caption("This workspace is completely empty and can be permanently deleted.")
                
                def cb_del_ws():
                    db_conn.execute("DELETE FROM workspaces WHERE id=%s", (workspace_id,))
                    db_conn.execute("DELETE FROM workspace_members WHERE workspace_id=%s", (workspace_id,))
                    db_conn.execute("DELETE FROM issue_categories WHERE workspace_id=%s", (workspace_id,))
                    db_conn.commit()
                    set_flash("Workspace Deleted", "warning")
                    
                st.button("❌ Delete Workspace", type="primary", disabled=not conf_ws, on_click=cb_del_ws)
        else:
            owner = ws_owner_data[0] if ws_owner_data else "Unknown"
            owner_display = get_user_display(owner, workspace_id)
            st.info(f"You are a member. Only the owner ({owner_display}) can modify or delete this workspace.")

    # --- LOCATIONS ---
    with t_loc:
        if not can_loc: st.warning("You do not have permission to manage Locations.")
        else:
            def cb_add_loc():
                new_loc = st.session_state.get("add_loc_name", "").strip()
                if new_loc:
                    if db_conn.execute("SELECT COUNT(*) FROM locations WHERE name=%s AND workspace_id=%s", (new_loc, workspace_id)).fetchone()[0] > 0:
                        set_flash("Location already exists!", "error")
                    else:
                        db_conn.execute("INSERT INTO locations (name, workspace_id, is_active) VALUES (%s, %s, 1)", (new_loc, workspace_id))
                        db_conn.commit()
                        st.session_state["add_loc_name"] = ""
                        set_flash("Location added")

            with st.form("new_loc", clear_on_submit=True):
                st.text_input("➕ Add New Location", key="add_loc_name")
                st.form_submit_button("Add Location", on_click=cb_add_loc)
                        
            st.write("### 📥 Bulk Import Locations")
            
            def cb_bulk_import_locs():
                loc_file = st.session_state.get("loc_bulk")
                if loc_file:
                    loc_df = pd.read_csv(loc_file) if loc_file.name.endswith('.csv') else pd.read_excel(loc_file)
                    imported_count = 0
                    if not loc_df.empty:
                        loc_col = loc_df.columns[0]
                        for idx, row in loc_df.iterrows():
                            val = str(row[loc_col]).strip()
                            if val and val != "nan":
                                if db_conn.execute("SELECT COUNT(*) FROM locations WHERE name=%s AND workspace_id=%s", (val, workspace_id)).fetchone()[0] == 0:
      
