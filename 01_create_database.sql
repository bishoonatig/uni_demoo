
-- 1. Doctors
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id SERIAL PRIMARY KEY,
    doctor_name VARCHAR(100) NOT NULL,
    department VARCHAR(100),
    phone VARCHAR(20)
);

-- 2. Rooms
CREATE TABLE IF NOT EXISTS rooms (
    room_id SERIAL PRIMARY KEY,
    room_name VARCHAR(50) NOT NULL,
    building VARCHAR(100),
    capacity INT CHECK (capacity > 0)
);

-- 3. Subjects
CREATE TABLE IF NOT EXISTS subjects (
    subject_id SERIAL PRIMARY KEY,
    subject_name VARCHAR(100) NOT NULL,
    doctor_id INT REFERENCES doctors(doctor_id)
);

-- 4. Lectures
CREATE TABLE IF NOT EXISTS lectures (
    lecture_id SERIAL PRIMARY KEY,
    subject_id INT REFERENCES subjects(subject_id),
    room_id INT REFERENCES rooms(room_id),
    lecture_day VARCHAR(20),
    start_time TIME,
    end_time TIME,
    CHECK (end_time > start_time)
);
