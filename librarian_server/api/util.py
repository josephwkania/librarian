"""
Web utils for v2 of the API that uses pydantic models.
"""

from pydantic import BaseModel

from flask import request, jsonify, Response
from typing import Optional

# TODO: Authentication

def pydantic_api(f, recieve_model: Optional[BaseModel] = None):
    """
    This decorator wraps API functions and serializes and deserializes
    them based upon the expected response types. 
    
    Crucially, if you provide a 'recieve_model' argument, a keyword
    argument of 'request' is provided to the function that is the
    deserialized request body.
    """

    def wrapped(*args, **kwargs):
        # If we have a recieve model, we need to deserialize the
        # request body into it.
        if recieve_model is not None:
            try:
                request_data = request.get_json()
            except:
                return jsonify({
                    "error": "Invalid JSON."
                }), 400

            try:
                request_model = recieve_model(**request_data)
            except:
                return jsonify({
                    "error": "Invalid request body."
                }), 400

            kwargs["request"] = request_model

        # Now run the function.
        try:
            result = f(*args, **kwargs)
        except Exception as e:
            return jsonify({
                "error": str(e)
            }), 500

        # If the result is a Response, just return it.
        if isinstance(result, Response):
            return result

        # If the result is a tuple, assume it is (data, status).
        if isinstance(result, tuple):
            data, status = result
        else:
            data, status = result, 200

        # If the data is a pydantic model, serialize it.
        if isinstance(data, BaseModel):
            data = dict(data)

        # Return the data.
        return jsonify(data), status

    return wrapped