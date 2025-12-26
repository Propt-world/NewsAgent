#!/bin/bash

# Create ECR repositories for NewsAgent services
aws ecr create-repository --repository-name newsagent-api --region us-east-1
aws ecr create-repository --repository-name newsagent-scheduler --region us-east-1  
aws ecr create-repository --repository-name newsagent-worker --region us-east-1

echo "ECR repositories created successfully"
