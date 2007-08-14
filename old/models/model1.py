from pony.main import *

class Group(Entity):
    number = Required(str)
    faculty = Required(int)
    graduate_year = Required(int)
    speciality = Required(int)
    subjects = Set('Subject')
    students = Set('Student')
    PrimaryKey(number, faculty)

class Subject(Entity):
    _table_ = 'DISCIPLINES'
    name = Unique(unicode)
    groups = Set(Group)
    marks = Set('Mark')

class Student(Entity):
    _tables_ = 'Student1', 'Student2'
    first_name = Required(unicode)
    mid_name = Optional(unicode, table='Student2')
    last_name = Required(unicode, table='Student1')
    group = Required(Group)
    marks = Set('Mark')
    PrimaryKey(group, first_name)

class Mark(Entity):
    student = Required(Student)
    subject = Required(Subject)
    value = Required(int)
    PrimaryKey(student, subject)
    