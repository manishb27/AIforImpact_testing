from fastapi import FastAPI, HTTPException, Query, File, UploadFile
from pydantic import BaseModel
from typing import Optional, Dict, List
import csv
import firebase_admin
from firebase_admin import credentials, firestore
from math import radians, cos, sin, sqrt, atan2
from datetime import datetime
from geopy.distance import geodesic
from fastapi.responses import JSONResponse
import io
import requests

# Initialize Firebase
cred = credentials.Certificate('key.json')
firebase_admin.initialize_app(cred)
db = firestore.client()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Initialize FastAPI
app = FastAPI()

# Helper functions
def serialize_firestore_document(doc):
    data = doc.to_dict()
    data['id']=doc.id
    for key, value in data.items():
        if isinstance(value, firestore.GeoPoint):
            data[key] = {
                "latitude": value.latitude,
                "longitude": value.longitude
            }
    return data

def is_within_radius(center, point, radius_km):
    center_coords = (center["latitude"], center["longitude"])
    point_coords = (point["latitude"], point["longitude"])
    return geodesic(center_coords, point_coords).km <= radius_km

# Pydantic schema
class ProductSchema(BaseModel):
    name: str
    description: str
    price: float
    quantity: int
    city: Optional[str]=None
    state: Optional[str]=None
    coordinates: Optional[Dict[str, float]]=None

@app.get("/products", response_model=list)
def get_all_products():
    try:
        products = db.collection('products').stream()
        product_list = [serialize_firestore_document(doc) for doc in products]
        return product_list
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/products/{product_id}")
def get_product_by_id(product_id: str):
    try:
        doc = db.collection('products').document(product_id).get()
        if doc.exists:
            return {"product": serialize_firestore_document(doc)}
        else:
            raise HTTPException(status_code=404, detail="Product not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Helper function to calculate distance between two geopoints
def calculate_distance(coord1, coord2):
    R = 6371  # Earth's radius in km
    lat1, lon1 = radians(coord1['latitude']), radians(coord1['longitude'])
    lat2, lon2 = radians(coord2['latitude']), radians(coord2['longitude'])
    
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    
    return R * c

@app.get("/products/search")
def search_products(
    name: Optional[str] = Query(None, description="Filter by product name"),
    city: Optional[str] = Query(None, description="Filter by city"),
    state: Optional[str] = Query(None, description="Filter by state"),):
    """
    Search products based on various filters.
    """

    try:
        print("Searching products...")
        query_ref = db.collection('products')

        # Apply filters
        if name:
            query_ref = query_ref.where('name', '==', name)
        if city:
            query_ref = query_ref.where('city', '==', city)
        if state:
            query_ref = query_ref.where('state', '==', state)
            
                        
        print("Executing Firestore query...")
        query = query_ref.stream()
        products = [serialize_firestore_document(doc) for doc in query if doc]

        print(f"Products fetched: {products}")

        if products:
            return {"products": products}
        else:
            raise HTTPException(status_code=404, detail="No products found with the given filters")

    except Exception as e:
        print(f"Error in search_products: {e}")
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {e}")


@app.post("/products")
async def create_product(product: ProductSchema):
    """
    Create a product, derive geopoint from city/state, and save it in Firestore.
    """
    try:
        product_dict = product.dict()

        # Ensure city or state is provided
        if 'city' in product_dict and product_dict['city']:
            location_query = product_dict['city']
        elif 'state' in product_dict and product_dict['state']:
            location_query = product_dict['state']
        else:
            raise HTTPException(
                status_code=400,
                detail="Either 'city' or 'state' must be provided to derive location."
            )

        # Derive geopoint from city/state using Google Maps API
        coordinates = await get_coordinates_endpoint(location_query)
        product_dict['coordinates'] = firestore.GeoPoint(
            coordinates['latitude'], coordinates['longitude']
        )

        # Add the product to Firestore
        doc_ref = db.collection('products').add(product_dict)
        return {
            "success": True,
            "message": "Product created successfully",
            "id": doc_ref[1].id
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/products/{product_id}")
def delete_product(product_id: str):
    try:
        doc_ref = db.collection('products').document(product_id)
        doc_ref.delete()
        return {"success": True, "message": "Product deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/products/bulk_upload")
async def bulk_upload_products(file: UploadFile = File(...)):
    if file.content_type != 'text/csv':
        raise HTTPException(status_code=400, detail="Invalid file type. Only CSV files are accepted.")

    try:
        products = []
        content = await file.read()
        decoded_content = content.decode("utf-8")
        csv_reader = csv.DictReader(decoded_content.splitlines())

        # Parse CSV and add to Firestore
        for row in csv_reader:
            # Initialize coordinates
            coordinates = None

            # If coordinates are provided in the CSV, use them
            if 'coordinates' in row and row['coordinates']:
                try:
                    coord = eval(row['coordinates'])  # Validate input carefully
                    coordinates = firestore.GeoPoint(coord['latitude'], coord['longitude'])
                    address = get_location_from_coordinates_endpoint(coord['latitude'], coord['longitude'])
                    row['city'] = address['city']
                    row['state'] = address['state']
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"Invalid coordinates format: {str(e)}")
            else:
                # If coordinates are not provided, derive them from city or state
                if 'city' in row and row['city']:
                    location_query = row['city']
                elif 'state' in row and row['state']:
                    location_query = row['state']
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Either 'coordinates', 'city', or 'state' must be provided for each product."
                    )

                # Fetch coordinates using the Google Maps API
                try:
                    geo_data = await get_coordinates_endpoint(location_query)
                    coordinates = firestore.GeoPoint(geo_data['latitude'], geo_data['longitude'])
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to derive coordinates: {str(e)}")

            # Populate row data
            row['coordinates'] = coordinates
            row['price'] = float(row['price'])
            row['quantity'] = int(row['quantity'])

            # Add derived city and state for reference if missing
            if 'city' not in row or not row['city']:
                row['city'] = await get_location_from_coordinates_endpoint(geo_data['latitude'], geo_data['longitude'])['city']
            if 'state' not in row or not row['state']:
                row['state'] = await get_location_from_coordinates_endpoint(geo_data['latitude'], geo_data['longitude'])['state']

            # Add to the list of products to upload
            products.append(row)

        # Batch write to Firestore for efficiency
        batch = db.batch()
        for product in products:
            doc_ref = db.collection('products').document()
            batch.set(doc_ref, product)
        batch.commit()

        return {"success": True, "message": f"{len(products)} products uploaded successfully."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

# Bulk upload endpoint for Researchers and NGOs
@app.post("/{collection_name}/bulk_upload")
async def bulk_upload(collection_name: str, file: UploadFile):
    if collection_name not in ["researchers", "ngos"]:
        raise HTTPException(status_code=400, detail="Invalid collection name")

    try:
        content = await file.read()
        csv_data = io.StringIO(content.decode())
        csv_reader = csv.DictReader(csv_data)

        records = []
        for row in csv_reader:
            try:
                # Parse location field into Firestore GeoPoint
                if "location" in row and row["location"]:
                    location = eval(row["location"])
                    row["location"] = firestore.GeoPoint(
                        location["latitude"], location["longitude"]
                    )
                else:
                    row["location"] = None
                records.append(row)
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Error parsing row: {row}. Error: {str(e)}",
                )

        # Write records to Firestore
        batch = db.batch()
        collection_ref = db.collection(collection_name)
        for record in records:
            doc_ref = collection_ref.document()
            batch.set(doc_ref, record)
        batch.commit()

        return JSONResponse(
            {"success": True, "message": f"Bulk upload to {collection_name} completed."}
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Search endpoint for Researchers and NGOs
@app.get("/{collection_name}/search")
async def search_collection(
    collection_name: str,
    description: Optional[str] = Query(None),
    focus_area: Optional[str] = Query(None),
    latitude: Optional[float] = Query(None),
    longitude: Optional[float] = Query(None),
    radius_km: Optional[float] = Query(None),
):
    if collection_name not in ["researchers", "ngos"]:
        raise HTTPException(status_code=400, detail="Invalid collection name")

    try:
        query_ref = db.collection(collection_name)
        docs = query_ref.stream()
        results = [serialize_firestore_document(doc) for doc in docs]

        # Pattern matching on description
        if description:
            description_lower = description.lower()
            results = [
                record
                for record in results
                if "description" in record
                and description_lower in record["description"].lower()
            ]

        # Pattern matching on focus_area
        if focus_area:
            focus_area_lower = focus_area.lower()
            results = [
                record
                for record in results
                if "focus_area" in record
                and focus_area_lower in record["focus_area"].lower()
            ]

        # Geolocation filtering
        if latitude is not None and longitude is not None and radius_km is not None:
            center = {"latitude": latitude, "longitude": longitude}
            results = [
                record
                for record in results
                if "location" in record
                and record["location"]
                and is_within_radius(center, record["location"], radius_km)
            ]

        return results if results else {"message": f"No {collection_name} found"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/{collection_name}/all")
async def get_all_documents(collection_name: str):
    """
    Fetch all documents from a specified collection (researchers or NGOs).
    """
    if collection_name not in ["researchers", "ngos"]:
        raise HTTPException(status_code=400, detail="Invalid collection name")

    try:
        # Fetch all documents from the specified collection
        docs = db.collection(collection_name).stream()
        results = [serialize_firestore_document(doc) for doc in docs]

        # Return the results
        if results:
            return {"success": True, "data": results}
        else:
            return {"success": False, "message": f"No {collection_name} found."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Endpoint to get coordinates from a location name
@app.get("/get-coordinates")
async def get_coordinates_endpoint(location: str = Query(..., description="Location name or address")):
    """
    Get latitude and longitude of a location using Google Maps Geocoding API.

    Args:
        location (str): The location name or address.

    Returns:
        dict: A dictionary with 'latitude' and 'longitude'.
    """
    try:
        url = f"https://maps.googleapis.com/maps/api/geocode/json"
        params = {"address": location, "key": GOOGLE_MAPS_API_KEY}
        response = requests.get(url, params=params)

        if response.status_code == 200:
            data = response.json()
            if data['status'] == 'OK':
                result = data['results'][0]
                coordinates = result['geometry']['location']
                return {
                    "latitude": coordinates['lat'],
                    "longitude": coordinates['lng']
                }
            else:
                raise HTTPException(status_code=400, detail=f"Error from API: {data['status']}")
        else:
            raise HTTPException(status_code=response.status_code, detail="Error fetching data from API")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Endpoint to get location from coordinates
@app.get("/get-location")
async def get_location_from_coordinates_endpoint(
    latitude: float = Query(..., description="Latitude of the location"),
    longitude: float = Query(..., description="Longitude of the location")
):
    """
    Get city and state from latitude and longitude using Google Maps API.

    Args:
        latitude (float): Latitude of the location.
        longitude (float): Longitude of the location.

    Returns:
        dict: A dictionary with 'city' and 'state'.
    """
    try:
        url = f"https://maps.googleapis.com/maps/api/geocode/json"
        params = {"latlng": f"{latitude},{longitude}", "key": GOOGLE_MAPS_API_KEY}
        response = requests.get(url, params=params)

        if response.status_code == 200:
            data = response.json()
            if data['status'] == 'OK':
                # Extract city and state
                result = data['results'][0]
                address_components = result['address_components']
                city = None
                state = None

                for component in address_components:
                    if "locality" in component['types']:  # City
                        city = component['long_name']
                    if "administrative_area_level_1" in component['types']:  # State
                        state = component['long_name']

                if city and state:
                    return {"city": city, "state": state}
                else:
                    raise HTTPException(status_code=404, detail="City or state not found in response")
            else:
                raise HTTPException(status_code=400, detail=f"Error from API: {data['status']}")
        else:
            raise HTTPException(status_code=response.status_code, detail="Error fetching data from API")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Run the application
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
