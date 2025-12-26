#!/bin/bash

ACCOUNT_ID=773672087130
REGION=us-east-1

# Login to ECR
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

# Build and tag images
docker build -t newsagent-api .
docker build -t newsagent-scheduler .
docker build -t newsagent-worker .

# Tag for ECR
docker tag newsagent-api:latest $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/newsagent-api:latest
docker tag newsagent-scheduler:latest $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/newsagent-scheduler:latest
docker tag newsagent-worker:latest $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/newsagent-worker:latest

# Push to ECR
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/newsagent-api:latest
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/newsagent-scheduler:latest
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/newsagent-worker:latest

echo "Images pushed to ECR successfully"
