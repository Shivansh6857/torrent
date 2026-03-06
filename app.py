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
    c.execute("INSERT INTO roles (role_name, permissions) VALUES ('Super Admin', %s) ON CONFLICT (role_name) DO NOTHING", (all_perms,))
        
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
                                    db_conn.execute("INSERT INTO locations (name, workspace_id, is_active) VALUES (%s, %s, 1)", (val, workspace_id))
                                    imported_count += 1
                        db_conn.commit()
                        set_flash(f"{imported_count} locations imported successfully.")
            
            st.file_uploader("Upload Excel/CSV with location names", type=['csv', 'xlsx'], key="loc_bulk")
            st.button("Import Locations", on_click=cb_bulk_import_locs)

            st.write("### Manage Existing Locations")
            
            def cb_upd_loc(loc_id, old_name, key):
                new_name = st.session_state.get(key)
                if new_name and new_name != old_name:
                    if db_conn.execute("SELECT COUNT(*) FROM locations WHERE name=%s AND workspace_id=%s", (new_name, workspace_id)).fetchone()[0] > 0:
                        set_flash("Name already taken.", "error")
                    else:
                        db_conn.execute("UPDATE locations SET name=%s WHERE id=%s", (new_name, loc_id))
                        db_conn.execute("UPDATE inventory SET location=%s WHERE location=%s AND workspace_id=%s", (new_name, old_name, workspace_id))
                        db_conn.execute("UPDATE issues SET location=%s WHERE location=%s AND workspace_id=%s", (new_name, old_name, workspace_id))
                        db_conn.commit()
                        set_flash(f"Renamed location to {new_name}")

            def cb_act_loc(loc_id, curr_state):
                db_conn.execute("UPDATE locations SET is_active=%s WHERE id=%s", (0 if curr_state == 1 else 1, loc_id))
                db_conn.commit()
                
            def cb_del_loc(loc_id):
                db_conn.execute("DELETE FROM locations WHERE id=%s", (loc_id,))
                db_conn.commit()
                set_flash("Deleted Location", "warning")

            locs = pd.read_sql("SELECT * FROM locations WHERE workspace_id=%s", db_conn.conn, params=(workspace_id,))
            for _, row in locs.iterrows():
                status_icon = "🟢" if row['is_active'] == 1 else "🔴"
                with st.expander(f"📍 {row['name']} {status_icon}"):
                    el1, el2 = st.columns([3, 1])
                    rnl_key = f"rnl_{row['id']}"
                    el1.text_input("Rename Location", value=row['name'], key=rnl_key)
                    
                    c_btn1, c_btn2 = st.columns(2)
                    c_btn1.button("💾 Update Name", key=f"upd_loc_{row['id']}", on_click=cb_upd_loc, args=(row['id'], row['name'], rnl_key))
                                
                    loc_has_inv = db_conn.execute("SELECT COUNT(*) FROM inventory WHERE location=%s AND workspace_id=%s", (row['name'], workspace_id)).fetchone()[0] > 0
                    loc_has_iss = db_conn.execute("SELECT COUNT(*) FROM issues WHERE location=%s AND workspace_id=%s", (row['name'], workspace_id)).fetchone()[0] > 0
                    conf_loc = c_btn2.checkbox("Confirm Action", key=f"conf_loc_{row['id']}")
                    
                    if loc_has_inv or loc_has_iss:
                        btn_label = "🔴 Deactivate" if row['is_active'] == 1 else "🟢 Reactivate"
                        c_btn2.button(btn_label, key=f"act_loc_{row['id']}", disabled=not conf_loc, on_click=cb_act_loc, args=(row['id'], row['is_active']))
                    else:
                        c_btn2.button("❌ Delete Location", key=f"del_loc_{row['id']}", type="primary", disabled=not conf_loc, on_click=cb_del_loc, args=(row['id'],))

            st.divider()
            st.write("### 🧹 Bulk Reset Location Data")
            all_locs = locs['name'].tolist()
            
            def cb_reset_locs():
                locs_to_reset = st.session_state.get("res_locs", [])
                if not st.session_state.get("res_conf"): set_flash("Please check the confirmation box.", "error")
                elif locs_to_reset:
                    cursor = db_conn.cursor()
                    for loc in locs_to_reset:
                        cursor.execute("DELETE FROM counts WHERE item_id IN (SELECT id FROM inventory WHERE location=%s AND workspace_id=%s)", (loc, workspace_id))
                        cursor.execute("DELETE FROM inventory WHERE location=%s AND workspace_id=%s", (loc, workspace_id))
                        cursor.execute("DELETE FROM issues WHERE location=%s AND workspace_id=%s", (loc, workspace_id))
                    db_conn.commit()
                    set_flash("Successfully wiped data.")
                else: set_flash("Select locations first.", "warning")

            with st.form("reset_locs", clear_on_submit=True):
                st.multiselect("Select Locations to Clear Data", all_locs, key="res_locs")
                st.checkbox("I confirm I want to PERMANENTLY wipe historical data for selected locations.", key="res_conf")
                st.form_submit_button("🚨 Reset Selected Locations", type="primary", on_click=cb_reset_locs)

    # --- ROLES & RBAC ---
    with t_role:
        if not can_role: st.warning("You do not have permission to manage Roles.")
        else:
            st.write("### ➕ Create New Role")
            st.caption("Roles are available globally but applied per-workspace.")
            
            def cb_add_role():
                r_name = st.session_state.get("nr_name")
                if r_name:
                    if db_conn.execute("SELECT COUNT(*) FROM roles WHERE role_name=%s", (r_name.strip(),)).fetchone()[0] > 0:
                        set_flash("Role already exists!", "error")
                    else:
                        new_perms = []
                        if st.session_state.get("nr_p1"): new_perms.append("Counting Portal")
                        if st.session_state.get("nr_p2"): new_perms.append("Standalone Issue Report")
                        if st.session_state.get("nr_p3"): new_perms.append("Dashboard & Export")
                        if st.session_state.get("nr_p4"): new_perms.append("Issue Reports")
                        if st.session_state.get("nr_p5"): new_perms.append("Data Export & Reports")
                        if st.session_state.get("nr_p6"): new_perms.append("Location Import")
                        
                        a_all = st.session_state.get("nr_a_all")
                        if a_all: new_perms.append("Masters & Settings")
                        if st.session_state.get("nr_a1") or a_all: new_perms.append("Manage Clients")
                        if st.session_state.get("nr_a2") or a_all: new_perms.append("Manage Locations")
                        if st.session_state.get("nr_a3") or a_all: new_perms.append("Manage Roles")
                        if st.session_state.get("nr_a4") or a_all: new_perms.append("Manage Users")
                        if st.session_state.get("nr_a5") or a_all: new_perms.append("Manage Categories")
                        if st.session_state.get("nr_a6") or a_all: new_perms.append("Manage System Settings")
                        
                        db_conn.execute("INSERT INTO roles (role_name, permissions) VALUES (%s,%s)", (r_name.strip(), json.dumps(new_perms)))
                        db_conn.commit()
                        st.session_state["nr_name"] = ""
                        set_flash("Role saved!")
                else: set_flash("Please enter a role name.", "error")

            with st.form("new_role_form", clear_on_submit=True):
                st.text_input("Role Name", key="nr_name")
                st.write("#### 🛡️ Page Permissions")
                c1, c2, c3 = st.columns(3)
                c1.checkbox("Counting Portal", value=True, key="nr_p1")
                c1.checkbox("Standalone Issue Report", value=True, key="nr_p2")
                c2.checkbox("Dashboard & Export", key="nr_p3")
                c2.checkbox("Issue Reports (Admin)", key="nr_p4")
                c3.checkbox("Data Export & Reports", key="nr_p5")
                c3.checkbox("Location Import", key="nr_p6")
                
                st.write("#### ⚙️ Admin Permissions")
                a_all = st.checkbox("Grant Full Masters & Settings Access", key="nr_a_all")
                ear1, ear2 = st.columns(2)
                ear1.checkbox("Manage Clients/Workspaces", disabled=a_all, key="nr_a1")
                ear1.checkbox("Manage Locations", disabled=a_all, key="nr_a2")
                ear1.checkbox("Manage Roles", disabled=a_all, key="nr_a3")
                ear2.checkbox("Manage Users", disabled=a_all, key="nr_a4")
                ear2.checkbox("Manage Categories", disabled=a_all, key="nr_a5")
                ear2.checkbox("Manage System Settings", disabled=a_all, key="nr_a6")

                st.form_submit_button("Save New Role", type="primary", on_click=cb_add_role)
                    
            st.write("### Manage Existing Roles")
            
            def cb_upd_role(role_name, key_dict):
                new_perms = []
                if st.session_state.get(key_dict['p1']): new_perms.append("Counting Portal")
                if st.session_state.get(key_dict['p2']): new_perms.append("Standalone Issue Report")
                if st.session_state.get(key_dict['p3']): new_perms.append("Dashboard & Export")
                if st.session_state.get(key_dict['p4']): new_perms.append("Issue Reports")
                if st.session_state.get(key_dict['p5']): new_perms.append("Data Export & Reports")
                if st.session_state.get(key_dict['p6']): new_perms.append("Location Import")
                
                a_all = st.session_state.get(key_dict['a_all'])
                if a_all: new_perms.append("Masters & Settings")
                if st.session_state.get(key_dict['a1']) or a_all: new_perms.append("Manage Clients")
                if st.session_state.get(key_dict['a2']) or a_all: new_perms.append("Manage Locations")
                if st.session_state.get(key_dict['a3']) or a_all: new_perms.append("Manage Roles")
                if st.session_state.get(key_dict['a4']) or a_all: new_perms.append("Manage Users")
                if st.session_state.get(key_dict['a5']) or a_all: new_perms.append("Manage Categories")
                if st.session_state.get(key_dict['a6']) or a_all: new_perms.append("Manage System Settings")
                
                db_conn.execute("UPDATE roles SET permissions=%s WHERE role_name=%s", (json.dumps(new_perms), role_name))
                db_conn.commit()
                set_flash("Role Updated!")
                
            def cb_del_role(role_name):
                db_conn.execute("DELETE FROM roles WHERE role_name=%s", (role_name,))
                db_conn.commit()
                set_flash("Deleted Role", "warning")

            roles_df = pd.read_sql("SELECT * FROM roles", db_conn.conn)
            for _, row in roles_df.iterrows():
                if row['role_name'] == 'Super Admin':
                    st.info("🛡️ Super Admin - Immutable Base Role")
                    continue
                    
                curr_perms = json.loads(row['permissions'])
                with st.expander(f"⚙️ {row['role_name']}"):
                    st.write("#### 🛡️ Page Permissions")
                    er1, er2, er3 = st.columns(3)
                    
                    kd = {
                        'p1': f"ep1_{row['role_name']}", 'p2': f"ep2_{row['role_name']}", 'p3': f"ep3_{row['role_name']}",
                        'p4': f"ep4_{row['role_name']}", 'p5': f"ep5_{row['role_name']}", 'p6': f"ep6_{row['role_name']}",
                        'a_all': f"ea_all_{row['role_name']}", 'a1': f"ea1_{row['role_name']}", 'a2': f"ea2_{row['role_name']}",
                        'a3': f"ea3_{row['role_name']}", 'a4': f"ea4_{row['role_name']}", 'a5': f"ea5_{row['role_name']}", 'a6': f"ea6_{row['role_name']}"
                    }
                    
                    er1.checkbox("Counting Portal", value=("Counting Portal" in curr_perms), key=kd['p1'])
                    er1.checkbox("Standalone Issue Report", value=("Standalone Issue Report" in curr_perms), key=kd['p2'])
                    er2.checkbox("Dashboard & Export", value=("Dashboard & Export" in curr_perms), key=kd['p3'])
                    er2.checkbox("Issue Reports (Admin)", value=("Issue Reports" in curr_perms), key=kd['p4'])
                    er3.checkbox("Data Export & Reports", value=("Data Export & Reports" in curr_perms), key=kd['p5'])
                    er3.checkbox("Location Import", value=("Location Import" in curr_perms), key=kd['p6'])
                    
                    st.write("#### ⚙️ Admin Permissions")
                    ea_all = st.checkbox("Grant Full Access", value=("Masters & Settings" in curr_perms), key=kd['a_all'])
                    ear1, ear2 = st.columns(2)
                    ear1.checkbox("Manage Clients", value=("Manage Clients" in curr_perms or ea_all), disabled=ea_all, key=kd['a1'])
                    ear1.checkbox("Manage Locations", value=("Manage Locations" in curr_perms or ea_all), disabled=ea_all, key=kd['a2'])
                    ear1.checkbox("Manage Roles", value=("Manage Roles" in curr_perms or ea_all), disabled=ea_all, key=kd['a3'])
                    ear2.checkbox("Manage Users", value=("Manage Users" in curr_perms or ea_all), disabled=ea_all, key=kd['a4'])
                    ear2.checkbox("Manage Categories", value=("Manage Categories" in curr_perms or ea_all), disabled=ea_all, key=kd['a5'])
                    ear2.checkbox("Manage System Settings", value=("Manage System Settings" in curr_perms or ea_all), disabled=ea_all, key=kd['a6'])

                    rb1, rb2 = st.columns(2)
                    rb1.button("💾 Update Role", key=f"upd_r_{row['role_name']}", on_click=cb_upd_role, args=(row['role_name'], kd))
                        
                    conf_role = rb2.checkbox("Confirm Action", key=f"conf_r_{row['role_name']}")
                    role_assigned = db_conn.execute("SELECT COUNT(*) FROM workspace_members WHERE role_name=%s", (row['role_name'],)).fetchone()[0] > 0
                    if role_assigned: rb2.error("Role actively assigned in workspaces.")
                    else:
                        rb2.button("❌ Delete Role", key=f"del_r_{row['role_name']}", type="primary", disabled=not conf_role, on_click=cb_del_role, args=(row['role_name'],))

    # --- USERS & INVITES ---
    with t_user:
        if not can_user: st.warning("You do not have permission to manage Users.")
        else:
            roles_list = pd.read_sql("SELECT role_name FROM roles", db_conn.conn)['role_name'].tolist()
            en_loc_assign_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'enable_location_assignment_{workspace_id}',)).fetchone()
            loc_assign_on = en_loc_assign_row[0] == '1' if en_loc_assign_row else False

            def cb_invite_user():
                ident = st.session_state.get("inv_ident", "").strip().lower()
                role = st.session_state.get("inv_role")
                if ident:
                    u_row = db_conn.execute("SELECT username FROM users WHERE LOWER(username)=%s OR LOWER(email)=%s", (ident, ident)).fetchone()
                    if not u_row:
                        set_flash("Could not find an account with that username or email.", "error")
                    else:
                        target_user = u_row[0]
                        if db_conn.execute("SELECT COUNT(*) FROM workspace_members WHERE username=%s AND workspace_id=%s", (target_user, workspace_id)).fetchone()[0] > 0:
                            set_flash(f"User '{target_user}' is already a member or has a pending invite.", "warning")
                        else:
                            db_conn.execute("INSERT INTO workspace_members (username, workspace_id, role_name, invite_status) VALUES (%s, %s, %s, 'pending')", (target_user, workspace_id, role))
                            db_conn.commit()
                            st.session_state["inv_ident"] = ""
                            set_flash(f"Invite sent to {target_user}!")

            st.write(f"### 🤝 Invite User to '{workspace_name}'")
            with st.form("invite_user", clear_on_submit=True):
                st.text_input("User's Exact Username OR Email", key="inv_ident")
                st.selectbox("Assign Role for this Workspace", roles_list, key="inv_role")
                st.form_submit_button("Send Invite", on_click=cb_invite_user)

            st.write("### Manage Workspace Members")
            
            def cb_upd_usr(uname, r_key, l_key):
                n_role = st.session_state.get(r_key)
                n_locs = st.session_state.get(l_key, [])
                db_conn.execute("UPDATE workspace_members SET role_name=%s, assigned_locations=%s WHERE username=%s AND workspace_id=%s", (n_role, json.dumps(n_locs), uname, workspace_id))
                db_conn.commit()
                set_flash("User updated!")
                
            def cb_rm_usr(uname):
                db_conn.execute("DELETE FROM workspace_members WHERE username=%s AND workspace_id=%s", (uname, workspace_id))
                db_conn.commit()
                set_flash("User removed from workspace.", "warning")

            members_df = pd.read_sql("SELECT wm.*, u.is_active FROM workspace_members wm JOIN users u ON wm.username = u.username WHERE wm.workspace_id=%s", db_conn.conn, params=(workspace_id,))
            
            for _, row in members_df.iterrows():
                u_status = "🟢" if row['is_active'] == 1 else "🔴 (Deact. Global)"
                if row['invite_status'] == 'pending': u_status = "🟡 (Pending Invite)"
                
                is_me = (row['username'] == st.session_state.username)
                display_name = get_user_display(row['username'], workspace_id)
                
                with st.expander(f"👤 {display_name} - ({row['role_name']}) {u_status}"):
                    r_key = f"r_{row['username']}"
                    if is_me: 
                        st.info("👑 This is you. You cannot alter your own role from this menu.")
                        st.session_state[r_key] = row['role_name'] # stub for callback
                    else:
                        st.selectbox("Change Role", roles_list, index=roles_list.index(row['role_name']) if row['role_name'] in roles_list else 0, key=r_key)
                    
                    locs_for_edit = pd.read_sql("SELECT name FROM locations WHERE is_active=1 AND workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist()
                    try: curr_locs = json.loads(row['assigned_locations'])
                    except: curr_locs = []
                    
                    l_key = f"loc_{row['username']}"
                    if loc_assign_on:
                        st.multiselect("Personal Locations", locs_for_edit, default=[l for l in curr_locs if l in locs_for_edit], key=l_key)
                    
                    ub1, ub2 = st.columns(2)
                    ub1.button("💾 Update User Settings", key=f"upd_{row['username']}", on_click=cb_upd_usr, args=(row['username'], r_key, l_key))
                        
                    if not is_me:
                        conf_usr = ub2.checkbox("Confirm Action", key=f"conf_rm_{row['username']}")
                        btn_txt = "❌ Revoke Invite" if row['invite_status'] == 'pending' else "❌ Remove Access"
                        ub2.button(btn_txt, key=f"rm_{row['username']}", type="primary", disabled=not conf_usr, on_click=cb_rm_usr, args=(row['username'],))

    # --- ISSUE CATEGORIES ---
    with t_iss:
        if not can_cat: st.warning("You do not have permission to manage Categories.")
        else:
            def cb_add_cat():
                new_cat = st.session_state.get("add_cat_name", "").strip()
                if new_cat:
                    if db_conn.execute("SELECT COUNT(*) FROM issue_categories WHERE name=%s AND workspace_id=%s", (new_cat, workspace_id)).fetchone()[0] > 0:
                        set_flash("Category already exists!", "error")
                    else:
                        db_conn.execute("INSERT INTO issue_categories (name, workspace_id, is_active) VALUES (%s, %s, 1)", (new_cat, workspace_id))
                        db_conn.commit()
                        st.session_state["add_cat_name"] = ""
                        set_flash("Added category.")

            with st.form("new_cat", clear_on_submit=True):
                st.text_input("➕ New Issue Category", key="add_cat_name")
                st.form_submit_button("Add Category", on_click=cb_add_cat)
                        
            st.write("### Manage Categories")
            
            def cb_upd_cat(cat_id, old_name, key):
                new_c_name = st.session_state.get(key)
                if new_c_name and new_c_name != old_name:
                    if db_conn.execute("SELECT COUNT(*) FROM issue_categories WHERE name=%s AND workspace_id=%s", (new_c_name, workspace_id)).fetchone()[0] > 0:
                        set_flash("Name already taken.", "error")
                    else:
                        db_conn.execute("UPDATE issue_categories SET name=%s WHERE id=%s", (new_c_name, cat_id))
                        db_conn.execute("UPDATE issues SET category=%s WHERE category=%s AND workspace_id=%s", (new_c_name, old_name, workspace_id))
                        db_conn.commit()
                        set_flash("Renamed Category")
                        
            def cb_act_cat(cat_id, curr_state):
                db_conn.execute("UPDATE issue_categories SET is_active=%s WHERE id=%s", (0 if curr_state == 1 else 1, cat_id))
                db_conn.commit()
                
            def cb_del_cat(cat_id):
                db_conn.execute("DELETE FROM issue_categories WHERE id=%s", (cat_id,))
                db_conn.commit()

            cats = pd.read_sql("SELECT * FROM issue_categories WHERE workspace_id=%s", db_conn.conn, params=(workspace_id,))
            for _, row in cats.iterrows():
                status_icon = "🟢" if row['is_active'] == 1 else "🔴"
                with st.expander(f"⚠️ {row['name']} {status_icon}"):
                    ec1, ec2 = st.columns([3, 1])
                    rnc_key = f"rnc_{row['id']}"
                    ec1.text_input("Rename Category", value=row['name'], key=rnc_key)
                    
                    c_btn1, c_btn2 = st.columns(2)
                    c_btn1.button("💾 Update Name", key=f"upd_cat_{row['id']}", on_click=cb_upd_cat, args=(row['id'], row['name'], rnc_key))
                    
                    cat_has_iss = db_conn.execute("SELECT COUNT(*) FROM issues WHERE category=%s AND workspace_id=%s", (row['name'], workspace_id)).fetchone()[0] > 0
                    conf_cat = c_btn2.checkbox("Confirm Action", key=f"conf_cat_{row['id']}")
                    
                    if cat_has_iss:
                        btn_label = "🔴 Deactivate" if row['is_active'] == 1 else "🟢 Reactivate"
                        c_btn2.button(btn_label, key=f"act_cat_{row['id']}", disabled=not conf_cat, on_click=cb_act_cat, args=(row['id'], row['is_active']))
                    else:
                        c_btn2.button("❌ Delete", key=f"del_cat_{row['id']}", type="primary", disabled=not conf_cat, on_click=cb_del_cat, args=(row['id'],))

    # --- SYSTEM SETTINGS ---
    with t_sys:
        if not can_sys: st.warning("You do not have permission to manage System Settings.")
        else:
            curr_timeout = int(get_setting(workspace_id, "inactive_timeout_mins", "5"))
            loc_assign_on = (get_setting(workspace_id, "enable_location_assignment", "0") == '1')
            curr_pref = get_setting(workspace_id, "display_pref", "Username")
            quick_entry_on = (get_setting(workspace_id, "quick_entry", "0") == '1')
            dec_places = int(get_setting(workspace_id, "decimal_places", "0"))
            
            req_iss_comm = (get_setting(workspace_id, "issue_req_comm", "1") == '1')
            req_iss_img = (get_setting(workspace_id, "issue_req_img", "0") == '1')
            hide_deact_dash = (get_setting(workspace_id, "hide_deact_loc_dash", "1") == '1')

            try: 
                vis_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'visible_columns_{workspace_id}',)).fetchone()
                curr_vis_cols = json.loads(vis_row[0]) if vis_row else []
            except: curr_vis_cols = []
            
            try: 
                drop_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'dropdown_columns_{workspace_id}',)).fetchone()
                curr_drop_cols = json.loads(drop_row[0]) if drop_row else []
            except: curr_drop_cols = []

            db_cols = [info[0] for info in db_conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'inventory'").fetchall()]
            hide_cols = ['id', 'item_code', 'item_name', 'location', 'workspace_id', 'book_qty', 'total_counted'] 
            avail_cols = [c for c in db_cols if c not in hide_cols]

            def cb_upd_settings():
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'inactive_timeout_mins_{workspace_id}', str(st.session_state.get("s_to"))))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'decimal_places_{workspace_id}', str(st.session_state.get("s_dec"))))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'quick_entry_{workspace_id}', '1' if st.session_state.get("s_quick") else '0'))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'issue_req_comm_{workspace_id}', '1' if st.session_state.get("s_req_c") else '0'))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'issue_req_img_{workspace_id}', '1' if st.session_state.get("s_req_i") else '0'))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'hide_deact_loc_dash_{workspace_id}', '1' if st.session_state.get("s_hide_d") else '0'))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'enable_location_assignment_{workspace_id}', '1' if st.session_state.get("s_loc_a") else '0'))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'display_pref_{workspace_id}', st.session_state.get("s_pref")))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'visible_columns_{workspace_id}', json.dumps(st.session_state.get("s_vis"))))
                db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'dropdown_columns_{workspace_id}', json.dumps(st.session_state.get("s_drop"))))
                db_conn.commit()
                set_flash("Settings Updated Successfully!")

            st.write("### General Settings")
            st.number_input("User Inactivity Timeout (Minutes)", value=curr_timeout, min_value=1, key="s_to")
            st.number_input("Stock Counting Decimal Places", value=dec_places, min_value=0, max_value=5, help="0 for whole numbers", key="s_dec")
            
            st.divider()
            st.write("### Issue Reporting Validations")
            st.checkbox("Make Comments Mandatory for Issue Reports", value=req_iss_comm, key="s_req_c")
            st.checkbox("Make Proof Image Mandatory for Issue Reports", value=req_iss_img, key="s_req_i")

            st.divider()
            st.write("### Workspace Display & UI")
            st.caption("How should users be identified across this workspace's reports and dashboards?")
            st.selectbox("Format", ["Username", "Email", "Display Name"], index=["Username", "Email", "Display Name"].index(curr_pref), key="s_pref")
            st.checkbox("Enable 'Quick Entry' on Counting Portal", value=quick_entry_on, help="Adds a quick quantity punch option next to the item header.", key="s_quick")
            st.checkbox("Hide deactivated locations from Dashboard", value=hide_deact_dash, key="s_hide_d")
            
            st.divider()
            st.write("### Access Control")
            st.checkbox("Enable Location-Based Access Control", value=loc_assign_on, key="s_loc_a")
            st.caption("If checked, users will only see locations explicitly assigned to them.")
            
            st.divider()
            st.write("### Extra Columns UI Display")
            st.multiselect("Columns to Display in Item HEADERs", options=avail_cols, default=[c for c in curr_vis_cols if c in avail_cols], key="s_vis")
            st.multiselect("Columns to Display INSIDE the Expander", options=avail_cols, default=[c for c in curr_drop_cols if c in avail_cols], key="s_drop")
            
            st.button("Update Settings", type="primary", on_click=cb_upd_settings)

# ==========================================
# 4. LOCATION-WISE IMPORT
# ==========================================
def location_import(workspace_id, workspace_name):
    st.header(f"📥 Stock Import for '{workspace_name}'")
    st.caption(f"🕒 {get_display_time(workspace_id)}")
    locations = pd.read_sql("SELECT name FROM locations WHERE is_active=1 AND workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist()
    
    if not locations:
        st.warning("No active locations found in workspace. You can still auto-create locations using the multi-location import.")

    is_multi_loc = st.toggle("🌐 Enable Multi-Location Import (Uses 'Location' column in file)")
    import_mode = st.radio(f"Import Mode:", [
        f"Wipe & Replace Stock", 
        f"Append additional items"
    ])
    
    target_loc = None
    if not is_multi_loc:
        if not locations: return
        target_loc = st.selectbox("📍 Target Location", locations)
        
    file = st.file_uploader("Upload Excel/CSV", type=['csv', 'xlsx'])
    if file:
        try:
            if file.name.endswith('.xlsx'):
                xls = pd.ExcelFile(file)
                if len(xls.sheet_names) > 1:
                    sel_sheet = st.selectbox("Select Sheet", xls.sheet_names)
                    df = pd.read_excel(file, sheet_name=sel_sheet)
                else:
                    df = pd.read_excel(file)
            else:
                df = pd.read_csv(file)
        except Exception as e:
            return st.error(f"Error reading file: {e}")
            
        cols = df.columns.tolist()
        
        c1, c2, c3, c4 = st.columns(4)
        m_code = c1.selectbox("Item Code", cols)
        m_name = c2.selectbox("Item Name", cols) 
        m_qty = c3.selectbox("Book Qty", cols)
        m_price = c4.selectbox("Price (Optional)", ["None"] + cols)
        
        m_loc = None
        if is_multi_loc:
            st.divider()
            m_loc = st.selectbox("Location Column", cols)
            auto_create_locs = st.checkbox("Auto-create missing locations from file", value=True)
            
        extra_cols = st.multiselect("Select Extra Columns to save", [c for c in cols if c not in [m_code, m_name, m_qty, m_price, m_loc]])

        if st.button(f"Execute Import", type="primary"):
            cursor = db_conn.cursor()
            cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'inventory'")
            existing_cols = [info[0] for info in cursor.fetchall()] 
            for col in extra_cols:
                if col not in existing_cols: cursor.execute(f'ALTER TABLE inventory ADD COLUMN "{col}" TEXT')

            if "Wipe" in import_mode:
                if is_multi_loc:
                    st.error("Wipe & Replace is not supported in Multi-Location Import mode. Please switch to Append.")
                    st.stop()
                else:
                    cursor.execute("DELETE FROM inventory WHERE location=%s AND workspace_id=%s", (target_loc, workspace_id))
                    cursor.execute("DELETE FROM counts WHERE item_id IN (SELECT id FROM inventory WHERE location=%s AND workspace_id=%s)", (target_loc, workspace_id))

            base_col_names = ["item_code", "item_name", "location", "workspace_id", "book_qty", "unit_price"]
            all_col_names = base_col_names + [f'"{c}"' for c in extra_cols]
            col_names_str = ", ".join(all_col_names)
            placeholders_str = ", ".join(["%s"] * len(all_col_names))
            insert_sql = f"INSERT INTO inventory ({col_names_str}) VALUES ({placeholders_str})"

            active_locs = set(pd.read_sql("SELECT name FROM locations WHERE workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist())

            for _, row in df.iterrows():
                p_val = safe_float(row[m_price]) if m_price != "None" else 0.0
                q_val = safe_float(row[m_qty])
                item_name_val = str(row[m_name]) if pd.notna(row[m_name]) else "Unknown Item"
                
                if is_multi_loc:
                    loc_val = str(row[m_loc]).strip()
                    if auto_create_locs and loc_val not in active_locs and pd.notna(row[m_loc]):
                        cursor.execute("INSERT INTO locations (name, workspace_id, is_active) VALUES (%s, %s, 1)", (loc_val, workspace_id))
                        active_locs.add(loc_val)
                    final_loc = loc_val
                else:
                    final_loc = target_loc
                    
                values = [str(row[m_code]), item_name_val, final_loc, workspace_id, q_val, p_val]
                for c in extra_cols: values.append(str(row[c]))
                cursor.execute(insert_sql, values)
            
            db_conn.commit()
            set_flash(f"✅ Data imported successfully!")
            st.rerun()

# ==========================================
# 5. ITERATIVE COUNTING PORTAL
# ==========================================
def counting_portal(workspace_id, workspace_name):
    st.header(f"📝 Counting Portal - {workspace_name}")
    st.caption(f"🕒 Current Time: **{get_display_time(workspace_id)}**")
    
    locations = get_allowed_locations(workspace_id)
    if not locations: return st.warning("You have no active assigned locations for this workspace.")
        
    sel_loc = st.selectbox("📍 Select Your Location", ["-- Select Location --"] + locations)
    if sel_loc == "-- Select Location --": return

    st.divider()
    search = st.text_input(f"🔍 Search within {sel_loc}")
    df_inv = pd.read_sql("SELECT * FROM inventory WHERE location=%s AND workspace_id=%s", db_conn.conn, params=(sel_loc, workspace_id))
    
    if df_inv.empty: return st.warning("No inventory found.")
    if search:
        mask = df_inv.astype(str).apply(lambda row: search.lower() in ' '.join(row).lower(), axis=1)
        df_inv = df_inv[mask]

    issue_categories = pd.read_sql("SELECT name FROM issue_categories WHERE is_active=1 AND workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist()

    try: 
        vis_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'visible_columns_{workspace_id}',)).fetchone()
        vis_cols = json.loads(vis_row[0]) if vis_row else []
    except: vis_cols = []
    try: 
        drop_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'dropdown_columns_{workspace_id}',)).fetchone()
        drop_cols = json.loads(drop_row[0]) if drop_row else []
    except: drop_cols = []

    ITEMS_PER_PAGE = 20
    total_items = len(df_inv)
    total_pages = max(1, math.ceil(total_items / ITEMS_PER_PAGE))
    
    c1, c2, c3 = st.columns([1, 2, 1])
    page_num = c2.number_input("Page", min_value=1, max_value=total_pages, value=1)
    
    start_idx = (page_num - 1) * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    df_page = df_inv.iloc[start_idx:end_idx]
    
    quick_entry_on = (get_setting(workspace_id, "quick_entry", "0") == '1')
    dec_places = int(get_setting(workspace_id, "decimal_places", "0"))
    step_val = 1.0 if dec_places == 0 else float(f"1e-{dec_places}")
    fmt = f"%.{dec_places}f"

    # Fetch and clear the force_open flag for this run
    force_open_id = st.session_state.pop('force_open_expander', None)

    # --- CALLBACKS FOR FLAWLESS CLEARING & SUBMISSION ---
    def cb_quick_entry(item_id, item_code, key):
        q_val = st.session_state.get(key, 0.0)
        if q_val > 0:
            db_conn.execute("INSERT INTO counts (item_id, \"user\", workspace_id, added_qty, timestamp, comment, image_data) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (item_id, st.session_state.username, workspace_id, q_val, get_current_time(workspace_id), "", None))
            db_conn.execute("UPDATE inventory SET total_counted = total_counted + %s WHERE id = %s", (q_val, item_id))
            db_conn.commit()
            st.session_state[f"rst_{item_id}"] = st.session_state.get(f"rst_{item_id}", 0) + 1
            set_flash(f"Added {q_val} to {item_code}", "toast")

    def cb_dropdown_count(item_id, nq_key, nc_key, img_key):
        val = st.session_state.get(nq_key, 0.0)
        comm = st.session_state.get(nc_key, "")
        img_file = st.session_state.get(img_key)
        
        if val > 0:
            db_conn.execute("INSERT INTO counts (item_id, \"user\", workspace_id, added_qty, timestamp, comment, image_data) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (item_id, st.session_state.username, workspace_id, val, get_current_time(workspace_id), comm, process_image(img_file)))
            db_conn.execute("UPDATE inventory SET total_counted = total_counted + %s WHERE id = %s", (val, item_id))
            db_conn.commit()
            st.session_state[f"rst_{item_id}"] = st.session_state.get(f"rst_{item_id}", 0) + 1
            set_flash("Saved count!", "toast")

    def cb_update_count(count_id, item_id, old_qty, eq_key, ec_key):
        new_qty = st.session_state.get(eq_key, 0.0)
        new_comm = st.session_state.get(ec_key, "")
        diff = new_qty - float(old_qty)
        db_conn.execute("UPDATE counts SET added_qty=%s, comment=%s WHERE id=%s", (new_qty, new_comm, count_id))
        db_conn.execute("UPDATE inventory SET total_counted = total_counted + %s WHERE id=%s", (diff, item_id))
        db_conn.commit()
        st.session_state['force_open_expander'] = item_id
        set_flash("Count updated!", "toast")

    def cb_dropdown_issue(item_id, loc, cat_key, comm_key, img_key, tab_key):
        cat = st.session_state.get(cat_key)
        comm = st.session_state.get(comm_key, "")
        img_file = st.session_state.get(img_key)
        
        req_c = get_setting(workspace_id, "issue_req_comm", "1") == '1'
        req_i = get_setting(workspace_id, "issue_req_img", "0") == '1'
        
        if req_c and not comm: 
            st.session_state[f"iss_err_{item_id}"] = "Please provide detailed comments."
            st.session_state['force_open_expander'] = item_id
        elif req_i and not img_file: 
            st.session_state[f"iss_err_{item_id}"] = "Please provide a proof image."
            st.session_state['force_open_expander'] = item_id
        else:
            db_conn.execute("INSERT INTO issues (item_id, location, workspace_id, \"user\", category, comment, image_data, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (item_id, loc, workspace_id, st.session_state.username, cat, comm, process_image(img_file), get_current_time(workspace_id)))
            db_conn.commit()
            st.session_state.pop(f"iss_err_{item_id}", None)
            st.session_state[tab_key] = "🔢 Stock Count" 
            st.session_state[f"rst_{item_id}"] = st.session_state.get(f"rst_{item_id}", 0) + 1
            set_flash("Issue Reported Successfully.", "toast")


    for _, row in df_page.iterrows():
        rst = st.session_state.setdefault(f"rst_{row['id']}", 0)
        
        header_text = f"📦 {row['item_code']} - {row['item_name']}"
        if vis_cols:
            extra_info = " | ".join([f"{c}: {row[c]}" for c in vis_cols if c in row and pd.notna(row[c])])
            if extra_info: header_text += f" | {extra_info}"
        
        display_total = format(row['total_counted'], f".{dec_places}f")
        display_book = format(row['book_qty'], f".{dec_places}f")
        header_text += f" | Total Counted: {display_total} / {display_book}"
        
        tab_key = f"tab_mem_{row['id']}"
        if tab_key not in st.session_state:
            st.session_state[tab_key] = "🔢 Stock Count"
            
        nq_key = f"n_q_{row['id']}_{rst}"
        nc_key = f"n_c_{row['id']}_{rst}"
        icomm_key = f"iss_comm_{row['id']}_{rst}"
        
        is_interacting = (
            (st.session_state.get(nq_key, 0.0) > 0) or 
            (st.session_state.get(nc_key, "") != "") or 
            (st.session_state.get(icomm_key, "") != "") or
            (st.session_state.get(tab_key) == "⚠️ Report Issue")
        )
        exp_kwargs = {}
        if is_interacting or force_open_id == row['id']:
            exp_kwargs['expanded'] = True

        if quick_entry_on:
            main_col, quick_col1, quick_col2 = st.columns([6, 1.5, 1])
            qk_key = f"qk_q_{row['id']}_{rst}"
            
            quick_col1.number_input("Qty", min_value=0.0, step=step_val, format=fmt, key=qk_key, label_visibility="collapsed")
            quick_col2.button("Add", key=f"qk_btn_{row['id']}_{rst}", type="primary", on_click=cb_quick_entry, args=(row['id'], row['item_code'], qk_key))
        else:
            main_col = st.container()

        with main_col:
            with st.expander(header_text, **exp_kwargs):
                if drop_cols:
                    drop_details = " | ".join([f"**{c}**: {row[c]}" for c in drop_cols if c in row and pd.notna(row[c])])
                    if drop_details: st.markdown(f"> *{drop_details}*")
                
                sel_tab = st.radio("Action", ["🔢 Stock Count", "⚠️ Report Issue"], horizontal=True, label_visibility="collapsed", key=tab_key)
                
                if sel_tab == "🔢 Stock Count":
                    user_counts = pd.read_sql("SELECT id, added_qty, timestamp, comment, image_data FROM counts WHERE item_id=%s AND \"user\"=%s AND workspace_id=%s ORDER BY id ASC", 
                                              db_conn.conn, params=(row['id'], st.session_state.username, workspace_id))
                    if not user_counts.empty:
                        st.write("**Your Previous Entries (Edit if needed):**")
                        for idx, u_count in user_counts.iterrows():
                            ec1, ec2, ec3 = st.columns([2, 2, 1])
                            eq_key = f"e_q_{u_count['id']}"
                            ec_key = f"e_c_{u_count['id']}"
                            
                            ec1.number_input(f"Entry {idx+1} ({u_count['timestamp']})", value=float(u_count['added_qty']), min_value=0.0, step=step_val, format=fmt, key=eq_key)
                            ec2.text_input("Note", value=str(u_count['comment'] if u_count['comment'] else ""), key=ec_key)
                            ec3.button("Update", key=f"e_btn_{u_count['id']}", on_click=cb_update_count, args=(u_count['id'], row['id'], u_count['added_qty'], eq_key, ec_key))
                        st.divider()

                    st.write("**➕ Add New Entry**")
                    nimg_key = f"img_{row['id']}_{rst}"
                    
                    nc1, nc2, nc3 = st.columns([1, 2, 2])
                    nc1.number_input("New Qty", min_value=0.0, step=step_val, format=fmt, key=nq_key)
                    nc2.text_input("Note", key=nc_key)
                    nc3.file_uploader("Attach Image", type=['png', 'jpg', 'jpeg'], key=nimg_key)
                    
                    st.button("Submit Count", key=f"n_btn_{row['id']}_{rst}", type="primary", on_click=cb_dropdown_count, args=(row['id'], nq_key, nc_key, nimg_key))

                elif sel_tab == "⚠️ Report Issue":
                    if st.session_state.get(f"iss_err_{row['id']}"):
                        st.error(st.session_state.pop(f"iss_err_{row['id']}"))

                    icat_key = f"iss_cat_{row['id']}_{rst}"
                    iimg_key = f"iss_img_{row['id']}_{rst}"
                    
                    st.selectbox("Issue Type", issue_categories, key=icat_key)
                    st.text_area("Detailed Comments", key=icomm_key)
                    st.file_uploader("Proof Image", type=['png', 'jpg', 'jpeg'], key=iimg_key)
                    
                    st.button("Submit Issue Report", key=f"iss_btn_{row['id']}_{rst}", type="primary", on_click=cb_dropdown_issue, args=(row['id'], sel_loc, icat_key, icomm_key, iimg_key, tab_key))

# ==========================================
# 6. STANDALONE ISSUE REPORTING
# ==========================================
def standalone_issue_report(workspace_id, workspace_name):
    st.header("🚨 Standalone Issue Reporting")
    st.caption(f"🕒 {get_display_time(workspace_id)}")
    locations = get_allowed_locations(workspace_id)
    if not locations: return st.warning("No assigned locations for this workspace.")
        
    cats = pd.read_sql("SELECT name FROM issue_categories WHERE is_active=1 AND workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist()
    
    try: 
        vis_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'visible_columns_{workspace_id}',)).fetchone()
        vis_cols = json.loads(vis_row[0]) if vis_row else []
    except: vis_cols = []

    with st.container(border=True):
        mode = st.radio("Is this item in our Database?", ["Yes", "No, I can't find it"])
        sel_loc = st.selectbox("📍 Location where issue was found", locations)
        
        db_id, unlisted_name = None, None
        if mode == "Yes":
            items = pd.read_sql("SELECT * FROM inventory WHERE location=%s AND workspace_id=%s", db_conn.conn, params=(sel_loc, workspace_id))
            if not items.empty:
                item_dict = {}
                for _, r in items.iterrows():
                    base_name = f"{r['item_code']} - {r['item_name']}"
                    extra_info = " | ".join([f"{c}: {r[c]}" for c in vis_cols if c in r and pd.notna(r[c])])
                    item_dict[f"{base_name} | {extra_info}" if extra_info else base_name] = r['id']
                sel_item = st.selectbox("📦 Select Item", list(item_dict.keys()))
                db_id = item_dict[sel_item]
        else:
            unlisted_name = st.text_input("📦 Type the Name/Description")

        def cb_standalone_issue():
            cat = st.session_state.get("sa_iss_cat")
            comm = st.session_state.get("sa_iss_comm", "")
            img_file = st.session_state.get("sa_iss_img")
            
            req_c = get_setting(workspace_id, "issue_req_comm", "1") == '1'
            req_i = get_setting(workspace_id, "issue_req_img", "0") == '1'
            
            if mode == "No, I can't find it" and not unlisted_name: 
                st.session_state["sa_iss_err"] = "Please provide the name."
            elif req_c and not comm: 
                st.session_state["sa_iss_err"] = "Please provide detailed comments."
            elif req_i and not img_file: 
                st.session_state["sa_iss_err"] = "Please provide a proof image."
            else:
                db_conn.execute("INSERT INTO issues (item_id, unlisted_item, location, workspace_id, \"user\", category, comment, image_data, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (db_id, unlisted_name, sel_loc, workspace_id, st.session_state.username, cat, comm, process_image(img_file), get_current_time(workspace_id)))
                db_conn.commit()
                st.session_state.pop("sa_iss_err", None)
                set_flash("Issue Reported!", "toast")

        with st.form("standalone_issue", clear_on_submit=True):
            if st.session_state.get("sa_iss_err"):
                st.error(st.session_state.pop("sa_iss_err"))
                
            st.selectbox("⚠️ Issue Category", cats, key="sa_iss_cat")
            st.text_area("Detailed Comments", key="sa_iss_comm")
            st.file_uploader("Proof Image", type=['png', 'jpg', 'jpeg'], key="sa_iss_img")
            st.form_submit_button("Submit Global Issue", type="primary", on_click=cb_standalone_issue)

# ==========================================
# 7. DASHBOARD 
# ==========================================
def combined_report(workspace_id, workspace_name):
    st.header(f"📊 Dashboard - {workspace_name}")
    st.caption(f"🕒 {get_display_time(workspace_id)}")
    
    st.subheader("👥 Workspace Activity Monitor")
    to_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'inactive_timeout_mins_{workspace_id}',)).fetchone()
    timeout_mins = int(to_row[0]) if to_row else 5
    
    user_logs_df = pd.read_sql("""
        SELECT c."user", c.timestamp, inv.location 
        FROM counts c LEFT JOIN inventory inv ON c.item_id = inv.id 
        WHERE c.workspace_id=%s ORDER BY c.timestamp ASC
    """, db_conn.conn, params=(workspace_id,))
    
    if not user_logs_df.empty:
        last_locs = user_logs_df.drop_duplicates(subset=['user'], keep='last')[['user', 'location']].rename(columns={'location': 'Last Location'})
        user_stats = user_logs_df.groupby('user').agg(last_active=('timestamp', 'max'), total_counts=('timestamp', 'count')).reset_index()
        user_stats = pd.merge(user_stats, last_locs, on='user', how='left')
    else:
        user_stats = pd.DataFrame(columns=['user', 'last_active', 'total_counts', 'Last Location'])

    all_ws_users = pd.read_sql("SELECT wm.username FROM workspace_members wm JOIN users u ON wm.username = u.username WHERE wm.workspace_id=%s AND u.is_active=1 AND wm.invite_status='accepted'", db_conn.conn, params=(workspace_id,))
    status_df = pd.merge(all_ws_users, user_stats, left_on='username', right_on='user', how='left').drop(columns=['user'])
    status_df['total_counts'] = status_df['total_counts'].astype(float).fillna(0).astype(int)

    simulated_now = datetime.strptime(get_current_time(workspace_id), "%Y-%m-%d %H:%M:%S")

    def eval_status(last_time):
        if pd.isna(last_time): return "🔴 Offline"
        try:
            last_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
            if (simulated_now - last_dt).total_seconds() / 60 <= timeout_mins: return "🟢 Active"
            return "🟡 Inactive"
        except: return "🟡 Inactive"

    status_df['Status'] = status_df['last_active'].apply(eval_status)
    status_df['Display Name'] = status_df['username'].apply(lambda u: get_user_display(u, workspace_id))
    
    status_df = status_df[['Display Name', 'Status', 'Last Location', 'total_counts', 'last_active']].rename(columns={'Display Name': 'User', 'total_counts': 'Counts', 'last_active': 'Last Log Time'})
    st.dataframe(status_df, width='stretch', hide_index=True)
    st.divider()

    df_inv = pd.read_sql("SELECT * FROM inventory WHERE workspace_id=%s", db_conn.conn, params=(workspace_id,))
    if df_inv.empty: return st.warning("No inventory data found for this workspace.")
    
    # Hide deactivated locations filter
    hide_deact = (get_setting(workspace_id, "hide_deact_loc_dash", "1") == '1')
    if hide_deact:
        active_locs = pd.read_sql("SELECT name FROM locations WHERE is_active=1 AND workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist()
        df_inv = df_inv[df_inv['location'].isin(active_locs)]
        if df_inv.empty: return st.warning("No active inventory locations found.")

    df_counts = pd.read_sql("SELECT item_id, \"user\", added_qty FROM counts WHERE workspace_id=%s", db_conn.conn, params=(workspace_id,))
    if not df_counts.empty:
        df_counts['user_display'] = df_counts['user'].apply(lambda u: get_user_display(u, workspace_id))
        user_pivot = df_counts.groupby(['item_id', 'user_display'])['added_qty'].sum().unstack(fill_value=0)
        user_pivot.columns = [f"User_{col}" for col in user_pivot.columns]
        user_pivot = user_pivot.reset_index()
        df_display = pd.merge(df_inv, user_pivot, left_on='id', right_on='item_id', how='left').drop(columns=['item_id'])
        for col in user_pivot.columns:
            if col != 'item_id': df_display[col] = df_display[col].fillna(0)
    else:
        df_display = df_inv.copy()

    df_display['Variance Qty'] = df_display['total_counted'] - df_display['book_qty']
    df_display['Variance Value'] = df_display['Variance Qty'] * df_display['unit_price']

    locations = ["All"] + df_display['location'].dropna().unique().tolist()
    sel_loc = st.selectbox("Filter Report by Location", locations)
    
    if sel_loc != "All": df_display = df_display[df_display['location'] == sel_loc]
    df_display = df_display.drop(columns=['id', 'workspace_id']) 
    
    c1, c2, c3 = st.columns(3)
    c1.metric(f"Items in {sel_loc}", len(df_display))
    
    dec_places = int(get_setting(workspace_id, "decimal_places", "0"))
    fmt = f",.{dec_places}f"
    c2.metric("Net Qty Variance", f"{df_display['Variance Qty'].sum():{fmt}}")
    c3.metric("Net Value Variance", f"₹{df_display['Variance Value'].sum():,.2f}")

    st.dataframe(df_display.style.map(lambda x: 'color: red' if str(x).startswith('-') else 'color: green', 
                                      subset=['Variance Qty', 'Variance Value']), width='stretch')

# ==========================================
# 8. ISSUE REPORTS VIEWER
# ==========================================
def issue_reports_page(workspace_id, workspace_name):
    st.header(f"⚠️ Submitted Issue Reports - {workspace_name}")
    st.caption(f"🕒 {get_display_time(workspace_id)}")
    
    issues_df = pd.read_sql("""
        SELECT iss.id, iss.item_id, iss.unlisted_item, inv.item_code, inv.item_name, iss.location, iss.category, iss.user, iss.timestamp, iss.comment, iss.image_data 
        FROM issues iss 
        LEFT JOIN inventory inv ON iss.item_id = inv.id 
        WHERE iss.workspace_id=%s
        ORDER BY iss.timestamp DESC
    """, db_conn.conn, params=(workspace_id,))
    
    if issues_df.empty: return st.info("No issues reported yet.")

    st.write("### Filter Reports")
    f_c1, f_c2, f_c3 = st.columns(3)
    
    loc_list = ["All"] + sorted(issues_df['location'].dropna().unique().tolist())
    cat_list = ["All"] + sorted(issues_df['category'].dropna().unique().tolist())
    
    filter_loc = f_c1.selectbox("By Location", loc_list)
    filter_cat = f_c2.selectbox("By Category", cat_list)
    filter_search = f_c3.text_input("🔍 Search Item Code or Notes")
    
    if filter_loc != "All": issues_df = issues_df[issues_df['location'] == filter_loc]
    if filter_cat != "All": issues_df = issues_df[issues_df['category'] == filter_cat]
    if filter_search:
        mask = issues_df.astype(str).apply(lambda row: filter_search.lower() in ' '.join(row).lower(), axis=1)
        issues_df = issues_df[mask]
        
    st.divider()
    st.write(f"Showing **{len(issues_df)}** reports.")

    try: 
        vis_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'visible_columns_{workspace_id}',)).fetchone()
        vis_cols = json.loads(vis_row[0]) if vis_row else []
    except: vis_cols = []

    for _, row in issues_df.iterrows():
        reporter_display = get_user_display(row['user'], workspace_id)
        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            with col1:
                if pd.notna(row['item_id']):
                    st.subheader(f"📦 {row['item_code']} - {row['item_name']}")
                    st.caption(f"**Location:** {row['location']}")
                    if vis_cols:
                        extra_data = pd.read_sql("SELECT * FROM inventory WHERE id=%s", db_conn.conn, params=(row['item_id'],)).iloc[0]
                        details = " | ".join([f"**{c}**: {extra_data[c]}" for c in vis_cols if c in extra_data and pd.notna(extra_data[c])])
                        if details: st.markdown(f"> *{details}*")
                else:
                    st.subheader(f"📦 UNLISTED: {row['unlisted_item']}")
                    st.caption(f"**Location:** {row['location']}")
                
                st.error(f"**Issue:** {row['category']}")
                st.write(f"**Reported By:** {reporter_display}  |  **Time:** {row['timestamp']}")
                st.write(f"**Notes:** {row['comment']}")
                
            with col2:
                if row['image_data']: st.image(base64.b64decode(row['image_data']), caption="Attached Proof", use_container_width=True)

# ==========================================
# 9. DATA EXPORT & REPORTS HUB
# ==========================================
def data_export_page(workspace_id, workspace_name):
    st.header(f"📁 Data Export Hub - {workspace_name}")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.write("### 📊 Workspace Excel Backup")
        inc_deact = st.checkbox("Include Data from Deactivated Locations", value=True)
        
        # Prepare Export DataFrame
        df_inv = pd.read_sql("SELECT * FROM inventory WHERE workspace_id=%s", db_conn.conn, params=(workspace_id,))
        df_counts = pd.read_sql("SELECT item_id, \"user\", added_qty, timestamp, comment FROM counts WHERE workspace_id=%s", db_conn.conn, params=(workspace_id,))
        
        if not inc_deact:
            active_locs = pd.read_sql("SELECT name FROM locations WHERE is_active=1 AND workspace_id=%s", db_conn.conn, params=(workspace_id,))['name'].tolist()
            df_inv = df_inv[df_inv['location'].isin(active_locs)]
            df_counts = df_counts[df_counts['item_id'].isin(df_inv['id'])]
        
        if not df_inv.empty:
            if not df_counts.empty:
                df_counts['user_display'] = df_counts['user'].apply(lambda u: get_user_display(u, workspace_id))
                
                user_pivot = df_counts.groupby(['item_id', 'user_display'])['added_qty'].sum().unstack(fill_value=0)
                user_pivot.columns = [f"User_{col}_Total" for col in user_pivot.columns]
                user_pivot = user_pivot.reset_index()
                
                df_counts['log_detail'] = df_counts.apply(
                    lambda r: f"{r['added_qty']} @ {r['timestamp']}" + (f" [{r['comment']}]" if pd.notna(r['comment']) and str(r['comment']).strip() else ""), 
                    axis=1
                )
                detail_pivot = df_counts.groupby(['item_id', 'user_display'])['log_detail'].apply(lambda x: "  |  ".join(x)).unstack(fill_value="")
                detail_pivot.columns = [f"User_{col}_Logs" for col in detail_pivot.columns]
                detail_pivot = detail_pivot.reset_index()

                df_export = pd.merge(df_inv, user_pivot, left_on='id', right_on='item_id', how='left').drop(columns=['item_id'])
                df_export = pd.merge(df_export, detail_pivot, left_on='id', right_on='item_id', how='left').drop(columns=['item_id'])
                
                for col in user_pivot.columns:
                    if col != 'item_id': df_export[col] = df_export[col].fillna(0)
                for col in detail_pivot.columns:
                    if col != 'item_id': df_export[col] = df_export[col].fillna("")
            else:
                df_export = df_inv.copy()
                
            df_export['Variance Qty'] = df_export['total_counted'] - df_export['book_qty']
            df_export['Variance Value'] = df_export['Variance Qty'] * df_export['unit_price']
            df_export = df_export.drop(columns=['id', 'workspace_id'])
        else:
            df_export = pd.DataFrame()
            
        # Export Presets feature
        st.divider()
        st.write("#### 📑 Column Layout & Preset Management")
        st.info("💡 **Tip for arranging columns:** The columns will export in the exact order you select them below. To move a column to the end, click the **'x'** next to its name to remove it, and then click the dropdown list to select it again.")
        
        all_export_cols = df_export.columns.tolist() if not df_export.empty else []
        presets_str = get_setting(workspace_id, "export_presets", "{}")
        try: presets = json.loads(presets_str)
        except: presets = {}
        if "Default" not in presets: presets["Default"] = all_export_cols

        preset_choice = st.selectbox("Select Column Layout Preset", list(presets.keys()) + ["-- Create New Preset --"])
        if preset_choice == "-- Create New Preset --":
            with st.form("new_preset", clear_on_submit=True):
                new_preset_name = st.text_input("New Preset Name")
                sel_cols = st.multiselect("Select & Arrange Columns to Export", all_export_cols, default=all_export_cols)
                if st.form_submit_button("Save Preset", type="secondary"):
                    if new_preset_name:
                        presets[new_preset_name] = sel_cols
                        db_conn.execute("INSERT INTO settings (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f"export_presets_{workspace_id}", json.dumps(presets)))
                        db_conn.commit()
                        st.success("Preset Saved!")
                    else: st.error("Please enter a name.")
        else:
            sel_cols = st.multiselect("Select & Arrange Columns to Export", all_export_cols, default=[c for c in presets[preset_choice] if c in all_export_cols])
        
        st.divider()

        if st.button("Generate Master Excel", type="primary"):
            if df_export.empty: st.warning("No data available to export.")
            else:
                df_export_final = df_export[sel_cols]
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    df_export_final.to_excel(writer, sheet_name="Master Inventory", index=False)
                    
                    counts_query = f"""
                        SELECT inv.item_code as 'Item Code', inv.item_name as 'Item Name', inv.location as 'Location', 
                               c."user" as 'System Username', c.added_qty as 'Qty Added', c.timestamp as 'Date & Time', c.comment as 'Notes'
                        FROM counts c
                        LEFT JOIN inventory inv ON c.item_id = inv.id
                        WHERE c.workspace_id=%s {"" if inc_deact else f"AND inv.location IN ('{chr(39).join(active_locs)}')"}
                        ORDER BY c.timestamp DESC
                    """
                    try: pd.read_sql(counts_query, db_conn.conn, params=(workspace_id,)).to_excel(writer, sheet_name="Detailed Count Logs", index=False)
                    except: pass
                    
                    issues_query = f"""
                        SELECT iss.id as 'Issue ID', inv.item_code as 'Item Code', inv.item_name as 'Item Name', 
                               iss.unlisted_item as 'Unlisted Item', iss.location as 'Location', iss.category as 'Category', 
                               iss."user" as 'System Username', iss.timestamp as 'Date & Time', iss.comment as 'Notes'
                        FROM issues iss
                        LEFT JOIN inventory inv ON iss.item_id = inv.id
                        WHERE iss.workspace_id=%s {"" if inc_deact else f"AND iss.location IN ('{chr(39).join(active_locs)}')"}
                        ORDER BY iss.timestamp DESC
                    """
                    try: pd.read_sql(issues_query, db_conn.conn, params=(workspace_id,)).to_excel(writer, sheet_name="Detailed Issue Logs", index=False)
                    except: pass
                
                st.download_button("📥 Download Workspace Excel", data=output.getvalue(), 
                                   file_name=f"{workspace_name}_Backup_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx", 
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")

    with col2:
        st.write("### 🖼️ Export Workspace Images")
        if st.button("Generate Image Archive"):
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
                c_df = pd.read_sql("SELECT id, item_id, image_data FROM counts WHERE workspace_id=%s AND image_data IS NOT NULL", db_conn.conn, params=(workspace_id,))
                for _, r in c_df.iterrows():
                    zip_file.writestr(f"Stock_Counts/Count_ID_{r['id']}_Item_{r['item_id']}.jpg", base64.b64decode(r['image_data']))
                
                i_df = pd.read_sql("SELECT id, category, image_data FROM issues WHERE workspace_id=%s AND image_data IS NOT NULL", db_conn.conn, params=(workspace_id,))
                for _, r in i_df.iterrows():
                    safe_cat = str(r['category']).replace(" ", "_")
                    zip_file.writestr(f"Issue_Reports/Issue_ID_{r['id']}_{safe_cat}.jpg", base64.b64decode(r['image_data']))
            
            st.download_button("📥 Download Image Archive (ZIP)", data=zip_buffer.getvalue(), 
                               file_name=f"{workspace_name}_Images_{datetime.now().strftime('%Y%m%d')}.zip", 
                               mime="application/zip", type="primary")

# ==========================================
# 10. AUTH & LOGIN LOGIC
# ==========================================
def execute_login(username):
    new_token = uuid.uuid4().hex
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_conn.execute("UPDATE users SET session_token=%s, last_login_time=%s WHERE username=%s", (new_token, now_str, username))
    db_conn.commit()
    
    st.session_state.logged_in = True
    st.session_state.username = username
    st.session_state.session_token = new_token
    if 'pending_login_user' in st.session_state: st.session_state.pop('pending_login_user')
    set_flash(f"Welcome back, {username}!")
    st.rerun()

# Execute flash display early so UI reacts
display_flash()

if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    if st.session_state.get('pending_login_user'):
        st.warning(f"⚠️ Account Access Collision")
        st.error(f"The user '{st.session_state.pending_login_user}' is currently logged in on another device. (Last Login: {st.session_state.pending_login_time})")
        st.write("Do you want to terminate their session and forcibly log in here?")
        c1, c2 = st.columns(2)
        if c1.button("✅ Yes, Force Login", type="primary"):
            execute_login(st.session_state.pending_login_user)
        if c2.button("❌ Cancel"):
            st.session_state.pop('pending_login_user')
            st.rerun()
    else:
        st.title("🛡️ Secure Stock Management")
        
        tab_login, tab_signup, tab_forgot = st.tabs(["🔐 Login", "📝 Sign Up", "❓ Forgot Details"])
        
        with tab_login:
            with st.form("login", clear_on_submit=True):
                u = st.text_input("Username OR Email")
                p = st.text_input("Password", type="password")
                if st.form_submit_button("Login"):
                    hp = hashlib.sha256(p.encode()).hexdigest()
                    ident = u.strip().lower()
                    
                    res = db_conn.execute("SELECT username, is_active, session_token, last_login_time FROM users WHERE (LOWER(username)=%s OR LOWER(email)=%s) AND password=%s", (ident, ident, hp)).fetchone()
                    
                    if res:
                        actual_uname = res[0]
                        if res[1] == 0: 
                            st.error("Account deactivated.")
                        elif res[2] is not None:
                            st.session_state.pending_login_user = actual_uname
                            st.session_state.pending_login_time = res[3]
                            st.rerun()
                        else: 
                            execute_login(actual_uname)
                    else: st.error("Invalid Credentials")
                    
        with tab_signup:
            with st.form("signup", clear_on_submit=True):
                st.write("Create your Account")
                c_u1, c_u2 = st.columns(2)
                new_fn = c_u1.text_input("First Name")
                new_ln = c_u2.text_input("Last Name")
                
                new_u = st.text_input("Choose Username *")
                new_e = st.text_input("Email Address *")
                
                c_p1, c_p2 = st.columns(2)
                new_p = c_p1.text_input("Choose Password *", type="password")
                new_cp = c_p2.text_input("Confirm Password *", type="password")
                
                st.divider()
                st.write("Account Recovery (Required)")
                sec_q = st.selectbox("Security Question *", [
                    "What was the name of your first pet?", 
                    "What is your mother's maiden name?", 
                    "What city were you born in?", 
                    "What was the model of your first car?"
                ])
                sec_a = st.text_input("Answer *")
                
                if st.form_submit_button("Sign Up"):
                    if not new_u or not new_e or not new_p or not new_cp or not sec_a: 
                        st.error("Fields marked with * are required.")
                    elif new_p != new_cp:
                        st.error("Passwords do not match.")
                    elif db_conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(username)=%s", (new_u.strip().lower(),)).fetchone()[0] > 0:
                        st.error("Username taken.")
                    elif db_conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(email)=%s", (new_e.strip().lower(),)).fetchone()[0] > 0:
                        st.error("Email is already registered.")
                    else:
                        hp = hashlib.sha256(new_p.encode()).hexdigest()
                        db_conn.execute("""
                            INSERT INTO users (username, email, first_name, last_name, password, security_question, security_answer, is_active) 
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 1)
                        """, (new_u.strip(), new_e.strip().lower(), new_fn.strip(), new_ln.strip(), hp, sec_q, sec_a.strip().lower()))
                        db_conn.commit()
                        st.success("Account created successfully! Please Log In.")

        with tab_forgot:
            sub_t1, sub_t2 = st.tabs(["Forgot Username", "Forgot Password"])
            
            with sub_t1:
                with st.form("forgot_uname", clear_on_submit=True):
                    rec_e = st.text_input("Enter your registered Email")
                    if st.form_submit_button("Recover Username"):
                        u_row = db_conn.execute("SELECT username FROM users WHERE LOWER(email)=%s", (rec_e.strip().lower(),)).fetchone()
                        if u_row:
                            st.success(f"**Your Username is:** {u_row[0]}")
                        else:
                            st.error("No account found with that email.")
            
            with sub_t2:
                if 'reset_user_match' not in st.session_state:
                    with st.form("forgot_pass_step1", clear_on_submit=True):
                        rec_ident = st.text_input("Enter your Username OR Email")
                        if st.form_submit_button("Find Account"):
                            ident = rec_ident.strip().lower()
                            u_row = db_conn.execute("SELECT username, security_question FROM users WHERE LOWER(username)=%s OR LOWER(email)=%s", (ident, ident)).fetchone()
                            if u_row:
                                st.session_state.reset_user_match = {"uname": u_row[0], "question": u_row[1]}
                                st.rerun()
                            else:
                                st.error("Account not found.")
                else:
                    st.write(f"Account found: **{st.session_state.reset_user_match['uname']}**")
                    with st.form("forgot_pass_step2", clear_on_submit=True):
                        st.info(f"**Security Question:** {st.session_state.reset_user_match['question']}")
                        ans = st.text_input("Your Answer")
                        new_p1 = st.text_input("New Password", type="password")
                        new_p2 = st.text_input("Confirm New Password", type="password")
                        
                        btn_c1, btn_c2 = st.columns(2)
                        if btn_c1.form_submit_button("Reset Password", type="primary"):
                            target_u = st.session_state.reset_user_match['uname']
                            db_ans = db_conn.execute("SELECT security_answer FROM users WHERE username=%s", (target_u,)).fetchone()[0]
                            
                            if not ans or ans.strip().lower() != db_ans.strip().lower():
                                st.error("Incorrect security answer.")
                            elif not new_p1 or new_p1 != new_p2:
                                st.error("Passwords do not match or are empty.")
                            else:
                                hp = hashlib.sha256(new_p1.encode()).hexdigest()
                                db_conn.execute("UPDATE users SET password=%s WHERE username=%s", (hp, target_u))
                                db_conn.commit()
                                del st.session_state.reset_user_match
                                st.success("Password reset successfully! Please log in.")
                                
                        if btn_c2.form_submit_button("Cancel"):
                            del st.session_state.reset_user_match
                            st.rerun()

else:
    db_token = db_conn.execute("SELECT session_token FROM users WHERE username=%s", (st.session_state.username,)).fetchone()[0]
    if db_token != st.session_state.get('session_token'):
        st.error("⚠️ Session Terminated: Accessed from another device.")
        if st.button("Return to Login"):
            st.session_state.clear()
            st.rerun()
        st.stop()
        
    # User Details for Sidebar Navigation
    u_details = db_conn.execute("SELECT first_name, last_name FROM users WHERE username=%s", (st.session_state.username,)).fetchone()
    fn, ln = u_details if u_details else ("", "")
    full_name = f"{fn} {ln}".strip()
    global_display_name = full_name if full_name else st.session_state.username

    # --- TENANT-AWARE WORKSPACE SELECTOR ---
    my_workspaces = db_conn.execute("""
        SELECT w.id, w.name, wm.role_name 
        FROM workspaces w 
        JOIN workspace_members wm ON w.id = wm.workspace_id 
        WHERE wm.username = %s AND w.is_active = 1 AND wm.invite_status = 'accepted'
    """, (st.session_state.username,)).fetchall()

    st.sidebar.title("Navigation")
    if full_name:
        st.sidebar.write(f"👤 **{global_display_name}** (@{st.session_state.username})")
    else:
        st.sidebar.write(f"👤 **{st.session_state.username}**")

    if not my_workspaces:
        st.sidebar.warning("No active workspaces.")
        st.write(f"### 🏢 Welcome to Stock Pro, {global_display_name}!")
        st.write("You are not part of any active workspaces yet. Create your first workspace or check your profile for pending invites.")
        
        st.session_state.permissions = []
        ac_id, ac_name = None, None
        can_admin = False
    else:
        ws_options = {f"{w[1]} ({w[0][:4]})": {"id": w[0], "name": w[1], "role": w[2]} for w in my_workspaces}
        
        if 'client_selector' not in st.session_state or st.session_state.client_selector not in ws_options:
            st.session_state.client_selector = list(ws_options.keys())[0]

        sel_label = st.sidebar.selectbox("🏢 Active Workspace", list(ws_options.keys()), key='client_selector')
        
        active_ws_data = ws_options[sel_label]
        ac_id = active_ws_data["id"]
        ac_name = active_ws_data["name"]
        st.session_state.role = active_ws_data["role"]
        
        role_data = db_conn.execute("SELECT permissions FROM roles WHERE role_name=%s", (st.session_state.role,)).fetchone()
        st.session_state.permissions = json.loads(role_data[0]) if role_data else []
        
        has_legacy = "Masters & Settings" in st.session_state.permissions
        is_super = st.session_state.role == 'Super Admin'
        has_any_admin = any(p in st.session_state.permissions for p in ["Manage Clients", "Manage Locations", "Manage Roles", "Manage Users", "Manage Categories", "Manage System Settings"])
        can_admin = is_super or has_legacy or has_any_admin
        
        st.sidebar.caption(f"Role here: **{st.session_state.role}**")

    st.sidebar.divider()

    # --- GLOBAL TIME OVERRIDE ---
    if can_admin and ac_id:
        override_status_row = db_conn.execute("SELECT value FROM settings WHERE key=%s", (f'override_active_{ac_id}',)).fetchone()
        override_status = (override_status_row[0] == '1') if override_status_row else False
        
        use_custom = st.sidebar.checkbox(f"⏰ Time Override", value=override_status)
        
        if use_custom:
            c_date = st.sidebar.date_input("Select Base Date")
            c_time_str = st.sidebar.text_input("Type Base Time (HH:MM:SS)", value="12:00:00")
            if st.sidebar.button("Sync & Run Global Time"):
                try:
                    c_time = datetime.strptime(c_time_str, "%H:%M:%S").time()
                    dt_obj = datetime.combine(c_date, c_time)
                    db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'override_active_{ac_id}', '1'))
                    db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'override_base_time_{ac_id}', dt_obj.strftime("%Y-%m-%d %H:%M:%S")))
                    db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'override_real_start_{ac_id}', datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    db_conn.commit()
                    set_flash("Time Synced!")
                    st.rerun()
                except ValueError: st.sidebar.error("Invalid time format.")
            if override_status: st.sidebar.caption("*Simulated clock is ticking globally...*")
        elif override_status: 
            db_conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f'override_active_{ac_id}', '0'))
            db_conn.commit()
            st.rerun()
            
    st.sidebar.divider()
    
    menu = []
    if ac_id:
        if "Counting Portal" in st.session_state.permissions: menu.append("📝 Counting Portal")
        if "Standalone Issue Report" in st.session_state.permissions: menu.append("🚨 Standalone Issue Report")
        if "Dashboard & Export" in st.session_state.permissions: menu.append("📊 Dashboard & Export")
        if "Issue Reports" in st.session_state.permissions: menu.append("⚠️ Issue Reports (Admin)")
        if "Location Import" in st.session_state.permissions: menu.append("📥 Location Import")
        if "Data Export & Reports" in st.session_state.permissions: menu.append("📁 Data Export & Reports")
        if can_admin: menu.append("⚙️ Masters & Settings")
            
    menu.append("👤 My Profile") 
    
    choice = st.sidebar.radio("Go to:", menu, key="main_sidebar_nav")
    
    if choice == "📝 Counting Portal": counting_portal(ac_id, ac_name)
    elif choice == "🚨 Standalone Issue Report": standalone_issue_report(ac_id, ac_name)
    elif choice == "📊 Dashboard & Export": combined_report(ac_id, ac_name)
    elif choice == "⚠️ Issue Reports (Admin)": issue_reports_page(ac_id, ac_name)
    elif choice == "📥 Location Import": location_import(ac_id, ac_name)
    elif choice == "📁 Data Export & Reports": data_export_page(ac_id, ac_name)
    elif choice == "⚙️ Masters & Settings": manage_masters_page(ac_id, ac_name)
    elif choice == "👤 My Profile": user_profile_page()
    
    st.sidebar.divider()
    if st.sidebar.button("🚪 Logout"):
        db_conn.execute("UPDATE users SET session_token=NULL WHERE username=%s", (st.session_state.username,))
        db_conn.commit()
        st.session_state.clear()
        st.rerun()
