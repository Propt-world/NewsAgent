# Deploying NewsAgent Worker to AWS

Since your infrastructure is already set up, here are the steps to build, push, and deploy your updated code.

## Option 1: CLI (Command Line)

### 1. Login to AWS ECR
Replace `us-east-1` and `123456789012` with your actual Region and Account ID.

```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com
```

### 2. Build and Push
**Important**: Since you are on a Mac, you must build for `linux/amd64`.

```bash
# 1. Build
docker build --platform linux/amd64 -t newsagent-worker .

# 2. Tag (Replace URI with your actual ECR Repo URI)
docker tag newsagent-worker:latest 123456789012.dkr.ecr.us-east-1.amazonaws.com/newsagent-worker:latest

# 3. Push
docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/newsagent-worker:latest
```

### 3. Update ECS Service
Force a new deployment to pull the latest image.

```bash
aws ecs update-service --cluster <YOUR_CLUSTER_NAME> --service <YOUR_SERVICE_NAME> --force-new-deployment
```

---

## Option 2: AWS Web Console

You still need to use your terminal to build/push the image (Steps 1 & 2 above), but you can trigger the deployment via the website.

### Step 1: Get Push Commands
1. Go to **Amazon ECR** > **Repositories**.
2. Click on your repository (e.g., `newsagent-worker`).
3. Click the **"View push commands"** button (top right).
4. Run the commands shown in your terminal.
   > **Note**: For the `docker build` command, manually add `--platform linux/amd64` to ensure it runs on AWS servers!

### Step 2: Force New Deployment
1. Go to **Amazon ECS** > **Clusters**.
2. Click on the cluster containing your service.
3. Click on the **Services** tab and select your worker service.
4. Click **Update**.
5. Look for the checkbox **"Force new deployment"** (usually under *Force new deployment* section or just a switch). **Check it.**
6. Click **Skip to review** (bottom).
7. Click **Update Service**.

ECS will now drain the old task and start a new one with your freshly pushed image.
