import os
import io
import uuid 
import threading # <--- WAS MISSING
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import qrcode
import pandas as pd

# --- TWILIO IMPORT ---
from twilio.rest import Client # <--- WAS MISSING

app = Flask(__name__)

# Database Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///attendance.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'super_secret_key'

# --- TWILIO CREDENTIALS ---
TWILIO_SID = 'AC60f78686f47e4b65f7f0af92bbc8de23' 
TWILIO_AUTH = '115603ed393e228e9eea97d777b6d4cb' 
TWILIO_PHONE = '+19786473736'                 

db = SQLAlchemy(app)

# --- MODELS ---
class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    grade = db.Column(db.String(20), nullable=False) 
    section = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(100), nullable=True)
    contact_number = db.Column(db.String(20), nullable=False)
    parent_name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200), nullable=True)
    qr_token = db.Column(db.String(100), unique=True, nullable=False)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    date = db.Column(db.Date, default=date.today)
    in_time = db.Column(db.DateTime, nullable=True)
    out_time = db.Column(db.DateTime, nullable=True)
    
    student = db.relationship('Student', backref=db.backref('attendances', lazy=True))

# Create DB Tables
with app.app_context():
    db.create_all()

# --- SMS HELPER FUNCTION ---
def send_sms_background(to_number, message_body):
    """
    Sends SMS in a background thread so the scanner doesn't freeze.
    """
    try:
        formatted_num = to_number.strip()
        # If necessary, add logic here to prepend country code if missing
        # e.g., if len(formatted_num) == 10: formatted_num = '+1' + formatted_num
        
        client = Client(TWILIO_SID, TWILIO_AUTH)
        
        message = client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE,
            to=formatted_num
        )
        print(f"SMS Sent to {formatted_num}: {message.sid}")
    except Exception as e:
        print(f"Failed to send SMS: {str(e)}")

# --- ROUTES ---

@app.route('/')
def index():
    today = date.today()
    present_now = Attendance.query.filter(
        Attendance.date == today, 
        Attendance.in_time.isnot(None), 
        Attendance.out_time.is_(None)
    ).all()
    
    all_today = Attendance.query.filter_by(date=today).order_by(Attendance.in_time.desc()).all()
    
    return render_template('index.html', present_now=present_now, all_today=all_today)

@app.route('/scan')
def scan():
    return render_template('scan.html')

@app.route('/process_qr', methods=['POST'])
def process_qr():
    data = request.json
    token = data.get('qr_data')
    
    if not token:
        return jsonify({'status': 'error', 'message': 'Invalid QR Data'}), 400

    student = Student.query.filter_by(qr_token=token).first()
    
    if not student:
        return jsonify({'status': 'error', 'message': 'Student not found!'}), 404

    today = date.today()
    record = Attendance.query.filter_by(student_id=student.id, date=today).first()
    
    timestamp = datetime.now()
    time_str = timestamp.strftime('%I:%M %p') 
    
    message = ""
    msg_type = ""
    sms_body = ""
    should_send_sms = False

    if not record:
        # CHECK IN
        new_record = Attendance(student_id=student.id, date=today, in_time=timestamp)
        db.session.add(new_record)
        message = f"Welcome, {student.name}! Checked IN at {time_str}"
        msg_type = "success"
        
        sms_body = f"ATTENDANCE ALERT: {student.name} has entered the campus at {time_str}."
        should_send_sms = True
        
    elif record.in_time and record.out_time is None:
        # CHECK OUT
        record.out_time = timestamp
        message = f"Goodbye, {student.name}! Checked OUT at {time_str}"
        msg_type = "warning"
        
        sms_body = f"ATTENDANCE ALERT: {student.name} has left the campus at {time_str}."
        should_send_sms = True
    else:
        # ALREADY COMPLETED
        message = f"{student.name}, attendance already completed for today."
        msg_type = "info"
        should_send_sms = False

    db.session.commit()

    # Trigger SMS in Background Thread
    if should_send_sms and student.contact_number:
        threading.Thread(
            target=send_sms_background, 
            args=(student.contact_number, sms_body)
        ).start()

    return jsonify({'status': 'success', 'message': message, 'type': msg_type})

# --- ADMIN & STUDENT MANAGEMENT ---

@app.route('/admin/', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        name = request.form['name']
        grade = request.form['grade']
        section = request.form['section']
        contact = request.form['contact']
        parent = request.form['parent']
        email = request.form.get('email', '')
        address = request.form.get('address', '')
        
        token = str(uuid.uuid4())
            
        new_student = Student(
            name=name, grade=grade, section=section, 
            contact_number=contact, parent_name=parent,
            email=email, address=address, qr_token=token
        )
        db.session.add(new_student)
        db.session.commit()
        return redirect(url_for('admin'))

    students = Student.query.all()
    return render_template('admin.html', students=students)

@app.route('/student/edit/<int:student_id>', methods=['GET', 'POST'])
def edit_student(student_id):
    student = Student.query.get_or_404(student_id)
    
    if request.method == 'POST':
        student.name = request.form['name']
        student.grade = request.form['grade']
        student.section = request.form['section']
        student.contact_number = request.form['contact']
        student.parent_name = request.form['parent']
        student.email = request.form.get('email')
        student.address = request.form.get('address')
        
        db.session.commit()
        return redirect(url_for('admin'))
        
    return render_template('edit_student.html', student=student)

@app.route('/student/delete/<int:student_id>')
def delete_student(student_id):
    student = Student.query.get_or_404(student_id)
    Attendance.query.filter_by(student_id=student.id).delete()
    db.session.delete(student)
    db.session.commit()
    return redirect(url_for('admin'))

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

# --- SEARCH, LIVE FEED & EXPORTS ---

@app.route('/view_students', methods=['GET'])
def view_students():
    search_term = request.args.get('search_term', '').strip()
    grade = request.args.get('grade', '')
    section = request.args.get('section', '').strip()
    parent = request.args.get('parent', '').strip()
    contact = request.args.get('contact', '').strip()
    email = request.args.get('email', '').strip()
    address = request.args.get('address', '').strip()

    query = Student.query

    if search_term:
        if search_term.isdigit():
            query = query.filter(Student.id == int(search_term))
        else:
            query = query.filter(Student.name.ilike(f"%{search_term}%"))
    if grade: query = query.filter(Student.grade == grade)
    if section: query = query.filter(Student.section.ilike(f"%{section}%"))
    if parent: query = query.filter(Student.parent_name.ilike(f"%{parent}%"))
    if contact: query = query.filter(Student.contact_number.ilike(f"%{contact}%"))
    if email: query = query.filter(Student.email.ilike(f"%{email}%"))
    if address: query = query.filter(Student.address.ilike(f"%{address}%"))

    students = query.all()
    return render_template('view_students.html', students=students)

@app.route('/export_page')
def export_page():
    return render_template('export.html')

@app.route('/download_report', methods=['POST'])
def download_report():
    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    grade_filter = request.form.get('grade')
    section_filter = request.form.get('section', '').strip()
    
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except ValueError:
        return "Invalid Date Format", 400

    query = Attendance.query.join(Student).filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date
    )
    
    if grade_filter: query = query.filter(Student.grade == grade_filter)
    if section_filter: query = query.filter(Student.section.ilike(f"%{section_filter}%"))
        
    records = query.order_by(Attendance.date.desc(), Attendance.in_time.asc()).all()
    
    data = []
    for r in records:
        data.append({
            'Date': r.date,
            'Name': r.student.name,
            'Grade': r.student.grade,
            'Section': r.student.section,
            'In Time': r.in_time.strftime('%H:%M:%S') if r.in_time else '-',
            'Out Time': r.out_time.strftime('%H:%M:%S') if r.out_time else '-',
            'Status': 'Completed' if r.out_time else 'Active',
            'Parent Name': r.student.parent_name,
            'Contact': r.student.contact_number
        })
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    if data:
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Report')
    else:
        return "No records found.", 404
    
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 
                     as_attachment=True, download_name=f"Attendance_{start_date}_to_{end_date}.xlsx")

@app.route('/live_feed')
def live_feed():
    return render_template('live_feed.html')

@app.route('/api/latest_attendee')
def get_latest_attendee():
    today = date.today()
    latest = Attendance.query.filter_by(date=today).order_by(Attendance.in_time.desc()).first()

    if latest:
        status = "Checked OUT" if latest.out_time else "Checked IN"
        time_display = latest.out_time if latest.out_time else latest.in_time
        return jsonify({
            'found': True,
            'name': latest.student.name,
            'grade': latest.student.grade,
            'section': latest.student.section,
            'id': latest.student.id,
            'time': time_display.strftime('%H:%M:%S'),
            'status': status
        })
    else:
        return jsonify({'found': False})

@app.route('/download_template')
def download_template():
    headers = {
        'Full Name': [], 'Grade': [], 'Section': [],
        'Parent Name': [], 'Contact Number': [], 'Email': [], 'Address': []
    }
    df = pd.DataFrame(headers)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Template')
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 
                     as_attachment=True, download_name='Student_Upload_Template.xlsx')

@app.route('/import_students', methods=['POST'])
def import_students():
    file = request.files['file']
    if not file: return "No file uploaded", 400

    try:
        df = pd.read_excel(file)
        for index, row in df.iterrows():
            if pd.isna(row['Full Name']) or pd.isna(row['Grade']): continue
            new_student = Student(
                name=str(row['Full Name']), grade=str(row['Grade']),
                section=str(row['Section']), parent_name=str(row['Parent Name']),
                contact_number=str(row['Contact Number']),
                email=str(row['Email']) if not pd.isna(row['Email']) else "",
                address=str(row['Address']) if not pd.isna(row['Address']) else "",
                qr_token=str(uuid.uuid4())
            )
            db.session.add(new_student)
        db.session.commit()
        return redirect(url_for('admin'))
    except Exception as e:
        return f"Error reading file: {str(e)}", 500

@app.route('/export_students')
def export_students():
    students = Student.query.all()
    data = []
    for s in students:
        data.append({
            'ID': s.id, 'Full Name': s.name, 'Grade': s.grade,
            'Section': s.section, 'Parent Name': s.parent_name,
            'Contact Number': s.contact_number, 'Email': s.email, 'Address': s.address
        })
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Students')
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 
                     as_attachment=True, download_name='Student_Master_List.xlsx')

@app.route('/export_filtered_students')
def export_filtered_students():
    search_term = request.args.get('search_term', '').strip()
    grade = request.args.get('grade', '')
    section = request.args.get('section', '').strip()
    parent = request.args.get('parent', '').strip()
    contact = request.args.get('contact', '').strip()
    email = request.args.get('email', '').strip()
    address = request.args.get('address', '').strip()

    query = Student.query
    if search_term:
        if search_term.isdigit(): query = query.filter(Student.id == int(search_term))
        else: query = query.filter(Student.name.ilike(f"%{search_term}%"))
    if grade: query = query.filter(Student.grade == grade)
    if section: query = query.filter(Student.section.ilike(f"%{section}%"))
    if parent: query = query.filter(Student.parent_name.ilike(f"%{parent}%"))
    if contact: query = query.filter(Student.contact_number.ilike(f"%{contact}%"))
    if email: query = query.filter(Student.email.ilike(f"%{email}%"))
    if address: query = query.filter(Student.address.ilike(f"%{address}%"))

    students = query.all()
    data = []
    for s in students:
        data.append({
            'ID': s.id, 'Full Name': s.name, 'Grade': s.grade,
            'Section': s.section, 'Parent Name': s.parent_name,
            'Contact Number': s.contact_number, 'Email': s.email, 'Address': s.address
        })
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Student List')
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 
                     as_attachment=True, download_name='Filtered_Student_List.xlsx')

if __name__ == '__main__':
    app.run(debug=True, port=5000)