# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import os
import re
import json
import sys
import boto3
import base64
import logging
from aws_embedded_metrics import metric_scope
from datetime import datetime, timedelta
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr
from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all

patch_all()

DdbImageTable = os.getenv('DdbImageTable')
StateMachineArn = os.getenv('StateMachineArn')

rek = boto3.client('rekognition')
dynamodb = boto3.resource("dynamodb")
dyn_table = dynamodb.Table(DdbImageTable)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

@xray_recorder.capture('## GetDynamo')
def GetImage(url):
    try:
        response = dyn_table.query(
        KeyConditionExpression=Key('img_url').eq(str(url))
        )
        return response

    except ClientError as e:
        logger.error(e.response['Error']['Message'])
        return {'Items': [], 'Count': 0}

@xray_recorder.capture('## AddDynamo')            
def AddImage(url,filename):
    try:
        epoch = datetime.utcfromtimestamp(0)
        epochexp = (datetime.now()+timedelta(days=15) - epoch).total_seconds() * 1000.0
        response = dyn_table.put_item(
        Item={
            'img_url': str(url),
            'filename': filename,
            'expire_at': int(epochexp)
            }
        )
        return response

    except ClientError as e:
        logger.error(e.response['Error']['Message'])
    return {'Items': [], 'Count': 0}

@xray_recorder.capture('## Calling StepFunction')
def CallStepFunction(tweet):
    client = boto3.client('stepfunctions') 
    response = client.start_execution(
        stateMachineArn=StateMachineArn,
        name=tweet["guidstr"],
        input=json.dumps(tweet)
    )
    AddImage(tweet["image_url"],tweet["guidstr"]+".csv")
    #logger.info(json.dumps(hdata))    

@metric_scope
def handler(event, context, metrics):
    skipped_count = 0
    processed_count = 0
    no_image = 0
    for rec in event:        
        if "extended_entities" in rec:            
            if "media" in rec["extended_entities"]:
                for m in rec["extended_entities"]["media"]:
                    if "media_url_https" in m:
                        if m["type"] == "photo":
                            dyn_resp = GetImage(m["media_url_https"])
                            if dyn_resp["Count"] == 0:
                                processed_count += 1
                                CallStepFunction({'guidstr': m["id_str"], 'full_text': rec["full_text"], 'image_url': m["media_url_https"]})
                            else:
                                skipped_count += 1                            
                        else:
                            no_image += 1
                    else:
                        no_image += 1
            else:
                no_image += 1
        else:
            no_image += 1


    metrics.set_namespace('TwitterRekognition')
    metrics.put_dimensions({"step": "Parser"})
    metrics.put_metric("TweetsProcessed", len(event), "Count")
    metrics.put_metric("ImagesIdentified", processed_count, "Count")
    metrics.set_property("RequestId", context.aws_request_id)
    metrics.set_property("LambdaName", context.function_name)
    metrics.set_property(
        "payload", { "tweets": str(len(event)) ,"processed": processed_count, "skipped": skipped_count, "no_image": no_image }
    )

    return True