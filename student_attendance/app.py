
import os
import io
import uuid 
import threading
import zipfile
import shutil
import pandas as pd  # <--- ADDED THIS
import qrcode        # <--- ADDED THIS
from datetime import datetime, date
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# --- TWILIO IMPORT ---
from twilio.rest import Client

# ==========================================
#  CREDENTIALS
# ==========================================
os.environ["TWILIO_ACCOUNT_SID"] = "AC60f78686f47e4b65f7f0af92bbc8de23"
os.environ["TWILIO_AUTH_TOKEN"] = "6663ed1eeca6441c16a89404df15d830" 
os.environ["TWILIO_PHONE_NUMBER"] = "+19786473736"
DEFAULT_COUNTRY_CODE = "+63" 

app = Flask(__name__)

# ==========================================
#  CONFIGURATION
# ==========================================
basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, 'attendance.db')

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'super_secret_key'

app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024 
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'zip'}

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

db = SQLAlchemy(app)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ==========================================
#  DATABASE MODELS
# ==========================================

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='sub_admin') # 'admin' or 'sub_admin'
    
    # PERMISSIONS (For Sub-Admins)
    can_settings = db.Column(db.Boolean, default=False) # Manage Grades, Sections, Terms
    can_backup = db.Column(db.Boolean, default=False)   # Download/Restore Backup
    can_archive = db.Column(db.Boolean, default=False)  # Delete/Restore/Archive Students

class GradeOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

class SectionOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

class AcademicTerm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    is_current = db.Column(db.Boolean, default=False) 

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_number = db.Column(db.String(50), unique=True, nullable=False) 
    name = db.Column(db.String(100), nullable=False)
    grade = db.Column(db.String(20), nullable=False) 
    section = db.Column(db.String(50), nullable=False)
    academic_term = db.Column(db.String(50), nullable=False) 
    email = db.Column(db.String(100), nullable=True)
    contact_number = db.Column(db.String(20), nullable=False)
    parent_name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200), nullable=True)
    photo_filename = db.Column(db.String(255), nullable=True) 
    qr_token = db.Column(db.String(100), unique=True, nullable=False)
    is_archived = db.Column(db.Boolean, default=False)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    date = db.Column(db.Date, default=date.today)
    in_time = db.Column(db.DateTime, nullable=True)
    out_time = db.Column(db.DateTime, nullable=True)
    student = db.relationship('Student', backref=db.backref('attendances', lazy=True))

with app.app_context():
    db.create_all()

# --- INITIAL DATA SEEDER ---
@app.before_request
def create_tables():
    # Default Super Admin (All Permissions Implicitly)
    if not User.query.first():
        default_admin = User(username='admin', password_hash=generate_password_hash('admin123'), role='admin', 
                             can_settings=True, can_backup=True, can_archive=True)
        db.session.add(default_admin)
        db.session.commit()
        print("Default Admin Created: admin / admin123")

    if not GradeOption.query.first():
        for i in range(1, 13): db.session.add(GradeOption(name=f"Grade {i}"))
        db.session.add(SectionOption(name="Section A"))
        db.session.add(SectionOption(name="Section B"))
        db.session.add(AcademicTerm(name=f"{date.today().year}-{date.today().year+1}", is_current=True))
        db.session.commit()

# --- CONTEXT PROCESSOR (Injects permissions into HTML) ---
@app.context_processor
def inject_globals():
    return dict(
        global_grades=GradeOption.query.order_by(GradeOption.name).all(),
        global_sections=SectionOption.query.order_by(SectionOption.name).all(),
        global_terms=AcademicTerm.query.order_by(AcademicTerm.name.desc()).all(),
        current_term=AcademicTerm.query.filter_by(is_current=True).first(),
        # Session Helpers
        current_user_name=session.get('username'),
        user_role=session.get('role'),
        can_settings=session.get('can_settings'),
        can_backup=session.get('can_backup'),
        can_archive=session.get('can_archive')
    )

# ==========================================
#  AUTH & PERMISSIONS
# ==========================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# SUPER ADMIN ONLY (User Management)
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            flash('Access denied. Super Admin privileges required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# SETTINGS PERMISSION CHECK
def settings_permission(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        if session.get('role') != 'admin' and not session.get('can_settings'):
            flash('You do not have permission to access Settings.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def home():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    return render_template('home.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            # Load permissions into session
            session['can_settings'] = user.can_settings or user.role == 'admin'
            session['can_backup'] = user.can_backup or user.role == 'admin'
            session['can_archive'] = user.can_archive or user.role == 'admin'
            
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

# --- USER MANAGEMENT (SUPER ADMIN ONLY) ---

@app.route('/users')
@admin_required
def manage_users():
    users = User.query.all()
    return render_template('users.html', users=users)

@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form['username']
    password = request.form['password']
    role = request.form['role'] 
    
    # Get Permissions from Checkboxes
    can_settings = 'perm_settings' in request.form
    can_backup = 'perm_backup' in request.form
    can_archive = 'perm_archive' in request.form

    if User.query.filter_by(username=username).first():
        flash('Username already exists.', 'error')
    else:
        new_user = User(
            username=username, 
            password_hash=generate_password_hash(password), 
            role=role,
            can_settings=can_settings,
            can_backup=can_backup,
            can_archive=can_archive
        )
        db.session.add(new_user)
        db.session.commit()
        flash('User created successfully.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/users/update_permissions/<int:user_id>', methods=['POST'])
@admin_required
def update_permissions(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('Cannot change permissions of Super Admin.', 'error')
    else:
        user.can_settings = 'perm_settings' in request.form
        user.can_backup = 'perm_backup' in request.form
        user.can_archive = 'perm_archive' in request.form
        db.session.commit()
        flash(f'Permissions updated for {user.username}.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/users/delete/<int:user_id>')
@admin_required
def delete_user(user_id):
    if user_id == session.get('user_id'):
        flash('Cannot delete yourself.', 'error')
    else:
        user = User.query.get_or_404(user_id)
        db.session.delete(user)
        db.session.commit()
        flash('User deleted.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/users/change_password/<int:user_id>', methods=['POST'])
@admin_required
def change_password(user_id):
    user = User.query.get_or_404(user_id)
    new_password = request.form['new_password']
    if len(new_password) < 4: flash('Password too short.', 'error')
    else:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash(f"Password for {user.username} updated.", 'success')
    return redirect(url_for('manage_users'))

# ==========================================
#  SMS HELPER
# ==========================================
def send_sms_background(to_number, message_body):
    try:
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        from_number = os.environ.get("TWILIO_PHONE_NUMBER")
        if not account_sid or not auth_token or not from_number: return
        client = Client(account_sid, auth_token)
        formatted_num = str(to_number).strip()
        if not formatted_num.startswith('+'):
            if formatted_num.startswith('0'): formatted_num = formatted_num[1:]
            formatted_num = DEFAULT_COUNTRY_CODE + formatted_num
        client.messages.create(body=message_body, from_=from_number, to=formatted_num)
    except Exception as e:
        print(f"Failed to send SMS to {to_number}: {str(e)}")

# ==========================================
#  BACKUP & RESTORE
# ==========================================
@app.route('/backup_system')
@login_required
def backup_system():
    # Check Permission
    if session.get('role') != 'admin' and not session.get('can_backup'):
        flash('Access Denied: You do not have backup privileges.', 'error')
        return redirect(url_for('settings'))

    try:
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(db_path): zf.write(db_path, arcname='attendance.db')
            uploads_path = app.config['UPLOAD_FOLDER']
            for root, dirs, files in os.walk(uploads_path):
                for file in files: zf.write(os.path.join(root, file), arcname=os.path.join('uploads', file))
        memory_file.seek(0)
        return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name=f'Backup_{datetime.now().strftime("%Y%m%d")}.zip')
    except Exception as e:
        flash(f"Backup failed: {str(e)}", "error")
        return redirect(url_for('settings'))

@app.route('/restore_system', methods=['POST'])
@login_required
def restore_system():
    if session.get('role') != 'admin' and not session.get('can_backup'):
        flash('Access Denied.', 'error')
        return redirect(url_for('settings'))

    file = request.files.get('backup_file')
    if file and file.filename.endswith('.zip'):
        try:
            db.session.remove()
            db.engine.dispose()
            with zipfile.ZipFile(file, 'r') as zf:
                temp_dir = os.path.join(basedir, 'temp_restore')
                if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
                os.makedirs(temp_dir)
                zf.extractall(temp_dir)
                if os.path.exists(os.path.join(temp_dir, 'attendance.db')): shutil.copy2(os.path.join(temp_dir, 'attendance.db'), db_path)
                if os.path.exists(os.path.join(temp_dir, 'uploads')):
                    if os.path.exists(app.config['UPLOAD_FOLDER']): shutil.rmtree(app.config['UPLOAD_FOLDER'])
                    shutil.move(os.path.join(temp_dir, 'uploads'), app.config['UPLOAD_FOLDER'])
                shutil.rmtree(temp_dir)
            flash('System restored successfully!', 'success')
        except Exception as e: flash(f'Restore failed: {str(e)}', 'error')
    return redirect(url_for('settings'))

# ==========================================
#  CORE ROUTES
# ==========================================

@app.route('/dashboard')
@login_required
def dashboard():
    today = date.today()
    active_term = AcademicTerm.query.filter_by(is_current=True).first()
    term_name = active_term.name if active_term else ""

    total_active = Student.query.filter_by(is_archived=False, academic_term=term_name).count()
    present_today = Attendance.query.filter_by(date=today).count()
    absent_today = max(0, total_active - present_today)

    current_year = today.year
    monthly_data = db.session.query(func.strftime('%m', Attendance.date), func.count(Attendance.id)).filter(func.strftime('%Y', Attendance.date) == str(current_year)).group_by(func.strftime('%m', Attendance.date)).all()

    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    monthly_attendance = [0] * 12
    monthly_absent = [0] * 12

    for month_str, count in monthly_data:
        idx = int(month_str) - 1
        monthly_attendance[idx] = count
        monthly_absent[idx] = max(0, (total_active * 22) - count)

    latest_logs = Attendance.query.filter_by(date=today).order_by(Attendance.in_time.desc()).limit(10).all()
    return render_template('index.html', pie_data=[present_today, absent_today], bar_labels=months, bar_present=monthly_attendance, bar_absent=monthly_absent, latest_logs=latest_logs, today_date=today)

@app.route('/scan')
def scan(): return render_template('scan.html')

@app.route('/process_qr', methods=['POST'])
def process_qr():
    data = request.json
    token = data.get('qr_data')
    if not token: return jsonify({'status': 'error', 'message': 'Invalid QR Data'}), 400

    student = Student.query.filter_by(qr_token=token, is_archived=False).first()
    if not student: return jsonify({'status': 'error', 'message': 'Student not found or Archived!'}), 404

    current_term_obj = AcademicTerm.query.filter_by(is_current=True).first()
    if not current_term_obj: return jsonify({'status': 'error', 'message': 'System Error: No Active Term.'}), 500
    
    if student.academic_term != current_term_obj.name:
        return jsonify({'status': 'error', 'message': f'Student is registered in {student.academic_term}, not current year.'}), 400

    today = date.today()
    record = Attendance.query.filter_by(student_id=student.id, date=today).first()
    timestamp = datetime.now()
    time_str = timestamp.strftime('%I:%M %p') 
    message, msg_type, sms_body, should_send_sms = "", "", "", False

    if not record:
        new_record = Attendance(student_id=student.id, date=today, in_time=timestamp)
        db.session.add(new_record)
        message = f"Welcome, {student.name}! Checked IN at {time_str}"
        msg_type, should_send_sms = "success", True
        sms_body = f"ATTENDANCE: {student.name} entered campus at {time_str}."
    elif record.in_time and record.out_time is None:
        record.out_time = timestamp
        message = f"Goodbye, {student.name}! Checked OUT at {time_str}"
        msg_type, should_send_sms = "warning", True
        sms_body = f"ATTENDANCE: {student.name} left campus at {time_str}."
    else:
        message, msg_type = f"{student.name}, already completed.", "info"

    db.session.commit()
    if should_send_sms and student.contact_number:
        threading.Thread(target=send_sms_background, args=(student.contact_number, sms_body)).start()

    return jsonify({'status': 'success', 'message': message, 'type': msg_type})

# ==========================================
#  SETTINGS (PROTECTED)
# ==========================================

@app.route('/settings')
@settings_permission # Custom Decorator
def settings(): return render_template('settings.html')

@app.route('/settings/add_grade', methods=['POST'])
@settings_permission
def add_grade():
    name = request.form.get('name').strip()
    if name and not GradeOption.query.filter_by(name=name).first():
        db.session.add(GradeOption(name=name)); db.session.commit(); flash('Grade added', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/delete_grade/<int:id>')
@settings_permission
def delete_grade(id):
    grade = GradeOption.query.get(id)
    if grade: db.session.delete(grade); db.session.commit(); flash('Grade deleted', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/add_section', methods=['POST'])
@settings_permission
def add_section():
    name = request.form.get('name').strip()
    if name and not SectionOption.query.filter_by(name=name).first():
        db.session.add(SectionOption(name=name)); db.session.commit(); flash('Section added', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/delete_section/<int:id>')
@settings_permission
def delete_section(id):
    section = SectionOption.query.get(id)
    if section: db.session.delete(section); db.session.commit(); flash('Section deleted', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/add_term', methods=['POST'])
@settings_permission
def add_term():
    name = request.form.get('name').strip()
    if name and not AcademicTerm.query.filter_by(name=name).first():
        db.session.add(AcademicTerm(name=name)); db.session.commit(); flash('Term added', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/delete_term/<int:id>')
@settings_permission
def delete_term(id):
    term = AcademicTerm.query.get(id)
    if term: 
        if term.is_current: flash('Cannot delete active term.', 'error')
        else: db.session.delete(term); db.session.commit(); flash('Term deleted', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/set_current_term/<int:id>')
@settings_permission
def set_current_term(id):
    db.session.query(AcademicTerm).update({AcademicTerm.is_current: False})
    term = AcademicTerm.query.get(id)
    term.is_current = True
    db.session.commit()
    flash(f'{term.name} is now active.', 'success')
    return redirect(url_for('settings'))

# ==========================================
#  ADMIN & IMPORT
# ==========================================

@app.route('/admin/', methods=['GET', 'POST'])
@login_required
def admin():
    current_term_obj = AcademicTerm.query.filter_by(is_current=True).first()
    if not current_term_obj:
        flash('Please set a Current Academic Term in Settings first.', 'error')
        return redirect(url_for('settings'))
    active_term_name = current_term_obj.name

    if request.method == 'POST':
        try:
            student_number = request.form['student_number']
            existing_student = Student.query.filter_by(student_number=student_number).first()

            photo_filename = None
            if 'photo' in request.files:
                file = request.files['photo']
                if file and allowed_file(file.filename):
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    new_filename = f"{uuid.uuid4()}.{ext}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
                    photo_filename = new_filename

            if existing_student:
                existing_student.name = request.form['name']
                existing_student.grade = request.form['grade']
                existing_student.section = request.form['section']
                existing_student.contact_number = request.form['contact']
                existing_student.parent_name = request.form['parent']
                existing_student.email = request.form.get('email', '')
                existing_student.address = request.form.get('address', '')
                existing_student.academic_term = active_term_name
                existing_student.is_archived = False
                
                if photo_filename:
                    if existing_student.photo_filename:
                        old_path = os.path.join(app.config['UPLOAD_FOLDER'], existing_student.photo_filename)
                        if os.path.exists(old_path): os.remove(old_path)
                    existing_student.photo_filename = photo_filename

                db.session.commit()
                flash(f'Student {existing_student.name} updated and moved to {active_term_name}.', 'success')
            else:
                new_student = Student(
                    student_number=student_number, name=request.form['name'], grade=request.form['grade'], 
                    section=request.form['section'], academic_term=active_term_name,
                    contact_number=request.form['contact'], parent_name=request.form['parent'], 
                    email=request.form.get('email', ''), address=request.form.get('address', ''), 
                    qr_token=str(uuid.uuid4()), photo_filename=photo_filename, is_archived=False
                )
                db.session.add(new_student)
                db.session.commit()
                flash('Student enrolled successfully!', 'success')

        except Exception as e:
             flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('admin'))

    students = Student.query.filter_by(is_archived=False, academic_term=active_term_name).order_by(Student.id.desc()).limit(30).all()
    return render_template('admin.html', students=students)

@app.route('/archive')
@login_required
def archive_page():
    # Permission Check for View Archive
    if session.get('role') != 'admin' and not session.get('can_archive'):
        flash('Access Denied: You do not have archive privileges.', 'error')
        return redirect(url_for('admin'))

    search_term = request.args.get('search_term', '').strip()
    current_term_obj = AcademicTerm.query.filter_by(is_current=True).first()
    active_term_name = current_term_obj.name if current_term_obj else ""
    query = Student.query.filter(or_(Student.is_archived == True, Student.academic_term != active_term_name))
    if search_term: query = query.filter(Student.name.ilike(f"%{search_term}%"))
    archived_students = query.all()
    return render_template('archive.html', students=archived_students, current_term_name=active_term_name)

@app.route('/student/archive/<int:student_id>')
@login_required
def archive_student(student_id):
    if session.get('role') != 'admin' and not session.get('can_archive'):
        flash('Access Denied.', 'error'); return redirect(url_for('admin'))
        
    student = Student.query.get_or_404(student_id)
    student.is_archived = True
    db.session.commit()
    flash(f'{student.name} archived.', 'info')
    return redirect(url_for('admin'))

@app.route('/student/restore/<int:student_id>')
@login_required
def restore_student(student_id):
    if session.get('role') != 'admin' and not session.get('can_archive'):
        flash('Access Denied.', 'error'); return redirect(url_for('archive_page'))

    student = Student.query.get_or_404(student_id)
    student.is_archived = False
    db.session.commit()
    flash(f'{student.name} restored.', 'success')
    return redirect(url_for('archive_page'))

@app.route('/student/transfer/<int:student_id>')
@login_required
def transfer_student(student_id):
    if session.get('role') != 'admin' and not session.get('can_archive'):
        flash('Access Denied.', 'error'); return redirect(url_for('archive_page'))

    student = Student.query.get_or_404(student_id)
    current_term_obj = AcademicTerm.query.filter_by(is_current=True).first()
    if current_term_obj:
        student.academic_term = current_term_obj.name
        student.is_archived = False
        db.session.commit()
        flash(f'{student.name} transferred to {current_term_obj.name} successfully!', 'success')
    else: flash('No active term set.', 'error')
    return redirect(url_for('archive_page'))

@app.route('/student/permanent_delete/<int:student_id>')
@login_required
def permanent_delete(student_id):
    if session.get('role') != 'admin' and not session.get('can_archive'):
        flash('Access Denied.', 'error'); return redirect(url_for('archive_page'))

    student = Student.query.get_or_404(student_id)
    if student.photo_filename:
        path = os.path.join(app.config['UPLOAD_FOLDER'], student.photo_filename)
        if os.path.exists(path): os.remove(path)
    Attendance.query.filter_by(student_id=student.id).delete()
    db.session.delete(student)
    db.session.commit()
    flash('Permanently deleted.', 'error')
    return redirect(url_for('archive_page'))

@app.route('/download_template')
@login_required
def download_template():
    headers = {'Student ID': [], 'Full Name': [], 'Grade': [], 'Section': [], 'Academic Term': [], 'Parent Name': [], 'Contact Number': [], 'Email': [], 'Address': [], 'Image Filename': []}
    df = pd.DataFrame(headers)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False, sheet_name='Template')
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name='Student_Upload_Template.xlsx')

@app.route('/import_students', methods=['POST'])
@login_required
def import_students():
    excel_file = request.files.get('file')
    image_files = request.files.getlist('bulk_images')
    if not excel_file: flash('No Excel file', 'error'); return redirect(url_for('admin'))

    current_term_obj = AcademicTerm.query.filter_by(is_current=True).first()
    if not current_term_obj: flash('Set Active Term first', 'error'); return redirect(url_for('admin'))
    active_term = current_term_obj.name

    image_map = {f.filename: f for f in image_files if f.filename}
    count = 0

    try:
        df = pd.read_excel(excel_file)
        for index, row in df.iterrows():
            if pd.isna(row['Full Name']) or pd.isna(row['Student ID']): continue
            def get_val(key): return str(row[key]).strip() if key in row and not pd.isna(row[key]) else ""

            st_id = get_val('Student ID')
            existing_student = Student.query.filter_by(student_number=st_id).first()

            saved_filename = None
            excel_img_name = get_val('Image Filename')
            if excel_img_name and excel_img_name in image_map:
                f = image_map[excel_img_name]
                if allowed_file(f.filename):
                    ext = f.filename.rsplit('.', 1)[1].lower()
                    new_filename = f"{uuid.uuid4()}.{ext}"
                    f.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
                    saved_filename = new_filename

            if existing_student:
                existing_student.name = get_val('Full Name')
                existing_student.grade = get_val('Grade')
                existing_student.section = get_val('Section')
                existing_student.parent_name = get_val('Parent Name')
                existing_student.contact_number = get_val('Contact Number')
                existing_student.email = get_val('Email')
                existing_student.address = get_val('Address')
                existing_student.academic_term = active_term
                existing_student.is_archived = False
                if saved_filename:
                    if existing_student.photo_filename:
                        old_path = os.path.join(app.config['UPLOAD_FOLDER'], existing_student.photo_filename)
                        if os.path.exists(old_path): os.remove(old_path)
                    existing_student.photo_filename = saved_filename
            else:
                new_student = Student(
                    student_number=st_id, name=get_val('Full Name'), grade=get_val('Grade'), section=get_val('Section'),
                    academic_term=active_term, parent_name=get_val('Parent Name'), contact_number=get_val('Contact Number'), 
                    email=get_val('Email'), address=get_val('Address'), qr_token=str(uuid.uuid4()), photo_filename=saved_filename, is_archived=False
                )
                db.session.add(new_student)
            count += 1
        db.session.commit()
        flash(f'Processed {count} students!', 'success')
    except Exception as e: flash(f'Import failed: {str(e)}', 'error')
    return redirect(url_for('admin'))

@app.route('/student/edit/<int:student_id>', methods=['GET', 'POST'])
@login_required
def edit_student(student_id):
    student = Student.query.get_or_404(student_id)
    if request.method == 'POST':
        student.student_number = request.form['student_number']
        student.name = request.form['name']
        student.grade = request.form['grade']
        student.section = request.form['section']
        student.academic_term = request.form['academic_term']
        student.contact_number = request.form['contact']
        student.parent_name = request.form['parent']
        student.email = request.form.get('email')
        student.address = request.form.get('address')
        if 'photo' in request.files:
            file = request.files['photo']
            if file and allowed_file(file.filename):
                if student.photo_filename:
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], student.photo_filename)
                    if os.path.exists(old_path): os.remove(old_path)
                ext = file.filename.rsplit('.', 1)[1].lower()
                new_filename = f"{uuid.uuid4()}.{ext}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
                student.photo_filename = new_filename
        db.session.commit()
        flash('Updated successfully', 'success')
        return redirect(url_for('admin'))
    return render_template('edit_student.html', student=student)

@app.route('/student/delete/<int:student_id>')
def delete_student(student_id): return redirect(url_for('archive_student', student_id=student_id))

@app.route('/generate_qr/<int:student_id>')
def generate_qr(student_id):
    student = Student.query.get_or_404(student_id)
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(student.qr_token)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    buffer = io.BytesIO()
    img.save(buffer, 'PNG')
    buffer.seek(0)
    return send_file(buffer, mimetype='image/png', as_attachment=True, download_name=f'{student.name}_qr.png')

@app.route('/view_students', methods=['GET'])
@login_required
def view_students():
    search_term = request.args.get('search_term', '').strip()
    grade = request.args.get('grade', '')
    section = request.args.get('section', '').strip()
    academic_term = request.args.get('academic_term', '').strip()
    
    current_term_obj = AcademicTerm.query.filter_by(is_current=True).first()
    active_term_name = current_term_obj.name if current_term_obj else ""
    if not academic_term: academic_term = active_term_name

    query = Student.query.filter_by(is_archived=False) 
    if search_term: query = query.filter((Student.student_number.ilike(f"%{search_term}%")) | (Student.name.ilike(f"%{search_term}%")))
    if grade: query = query.filter(Student.grade == grade)
    if section: query = query.filter(Student.section == section)
    if academic_term: query = query.filter(Student.academic_term == academic_term)
    students = query.all()
    return render_template('view_students.html', students=students)

@app.route('/export_page')
@login_required
def export_page(): return render_template('export.html')

@app.route('/download_report', methods=['POST'])
@login_required
def download_report():
    try:
        start = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
        end = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d').date()
        query = Attendance.query.join(Student).filter(Attendance.date >= start, Attendance.date <= end)
        records = query.order_by(Attendance.date.desc()).all()
        data = [{'Date': r.date, 'Student ID': r.student.student_number, 'Name': r.student.name, 'In': r.in_time, 'Out': r.out_time} for r in records]
        df = pd.DataFrame(data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name='Report.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except: return "Error", 400

@app.route('/live_feed')
def live_feed(): return render_template('live_feed.html')

@app.route('/api/latest_attendee')
def get_latest_attendee():
    today = date.today()
    latest = Attendance.query.filter_by(date=today).order_by(Attendance.in_time.desc()).first()
    if latest:
        status = "Checked OUT" if latest.out_time else "Checked IN"
        time_display = latest.out_time if latest.out_time else latest.in_time
        photo_url = f"https://ui-avatars.com/api/?name={latest.student.name}&background=3B82F6&color=fff&size=400"
        if latest.student.photo_filename:
            photo_url = url_for('static', filename=f'uploads/{latest.student.photo_filename}')
        return jsonify({
            'found': True, 'name': latest.student.name, 'grade': latest.student.grade,
            'section': latest.student.section, 'time': time_display.strftime('%H:%M:%S'),
            'status': status, 'photo_url': photo_url, 'student_number': latest.student.student_number
        })
    return jsonify({'found': False})

@app.route('/export_students')
@login_required
def export_students():
    current_term_obj = AcademicTerm.query.filter_by(is_current=True).first()
    term = current_term_obj.name if current_term_obj else ""
    students = Student.query.filter_by(is_archived=False, academic_term=term).all()
    data = [{'Student ID': s.student_number, 'Full Name': s.name, 'Grade': s.grade} for s in students]
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name='Students.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/export_filtered_students')
def export_filtered_students(): return "Not Implemented"

if __name__ == '__main__':
    app.run(debug=True, port=5000)