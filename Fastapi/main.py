from fastapi import FastAPI, Path, HTTPException, Query
import json

app = FastAPI()


def load_data():
    with open('patients.json','r') as f:
        data = json.load(f)

    return data    

@app.get("/")
def hello():
    return {"message": "Patient Management"}

@app.get("/about")
def ideA():
    return{"message":"We keep the details about patient, so you can focus on improving your health"}

@app.get('/view')
def view():
    data = load_data()

    return data    

@app.get('/patient/{patient_id}')
def view_patient(
    patient_id: str = Path(
        ...,
        description="ID of the patient in the DB",
        example="P001",
    ),
):
    # load all patients
    data = load_data()

    if patient_id in data:
        return data[patient_id]
    # return {'error': 'patient not found'}    
    raise HTTPException(status_code=404, detail="Patient not found")

@app.get('/sort')
def sort_patients(
    sort_by: str = Query(
        ...,            
        description="Field to sort by",
        example="BMI,Height,Weight",
    ),
    sort_order: str = Query(
        'asc',
        description="Sort order",
        example="asc",
    ),
):
    valid_fields = ['bmi', 'height', 'weight']

    if sort_by not in valid_fields:
        raise HTTPException(status_code = 400, detail = f'Invalid field{valid_fields}')

    if sort_order not in ['asc','desc']:
        raise HTTPException(status_code=400, detail= 'Invalid order select one from [ASC] or [DESC]')

    data = load_data()

    sorted_order = True if sort_order == 'desc' else False

    sorted_data = sorted(data.values(), key = lambda x: x.get(sort_by,0),reverse = sorted_order)  

    return sorted_data
