# Common Commands 


Running the server locally 

```
python file-server/file-server.py
```

Calling my code locally 

```
curl -i http://localhost:8080/<a-real-file>.html

curl -i http://localhost:8080/does-not-exist.html

curl -i -X POST http://localhost:8080/ \
    -H "Content-Type: application/json" \
    -d '{"filename": "399.html"}'

curl -X POST -H "X-country: Iran" -H "Content-Type: application/json" -d '{"filename": "3000.html"}' http://localhost:8080/300.html

curl -i -H "X-country: Iran" http://localhost:8080/300.html
```

Creating my VM with my service account. 

```
gcloud compute instances create my-vm \
    --zone=us-central1-a \
    --address=homework4-ip \
    --service-account=homework5-service-account@project-8aeecca0-4f70-4c6b-8e3.iam.gserviceaccount.com \
    --scopes=cloud-platform \
    --metadata-from-file=startup-script=file-server/startup.sh,file-server-py=file-server/file-server.py
```

Calling my server on the VM.

```
curl -i http://136.64.134.90:8080/5000.html

curl -i -X POST http://136.64.134.90:8080 \
  -H "Content-Type: application/json" \
  -d '{"filename": "1164.html"}'

curl -i -X PUT http://136.64.134.90:8080 \
  -H "Content-Type: application/json" \
  -d '{"filename": "1164.html"}'

curl -i -X POST http://136.64.134.90:8080 \
  -H "Content-Type: application/json" \
  -H "X-country: Syria" \
  -d '{"filename": "1164.html"}'


curl -H "X-country: France" -H "X-client-IP: 1.2.3.4" -H "X-gender: Male" \
     -H "X-age: 17-25" -H "X-income: 20k-40k" -H "X-time: 13:00:00" \
     http://136.64.134.90:8080/1.html


curl -H "X-country: Iran" -H "X-client-IP: 1.2.3.4" -H "X-gender: Male" \
     -H "X-age: 17-25" -H "X-income: 20k-40k" -H "X-time: 13:00:00" \
     http://136.64.134.90:8080/1.html

./http-client --domain 136.64.134.90 --port 8080 --num_requests 10 -i 9999 --bucket none --webdir none --verbose

http://136.64.134.90:8080/8291.html
```

SSH into my server in google cloud

```
gcloud compute ssh my-vm --zone=us-central1-a
```

Puts the startup script and the python server into google clouds metadata.
startup script is called on startup, file server is called by the startup script.

```
gcloud compute instances add-metadata my-vm \
    --zone=us-central1-a \
    --metadata-from-file=startup-script=file-server/startup.sh,file-server-py=file-server/file-server.py
```


Resetting my VM when I need to re-run the startup script

```
gcloud compute instances reset my-vm --zone=us-central1-a
```

Stopping my server when I am done with it since I don't want to spend a lot: 

```
gcloud compute instances stop my-vm --zone=us-central1-a
```

# Starting up a new VM running my HTTP Client 

```
gcloud compute instances create client-vm \
    --zone=us-central1-a \
    --machine-type=e2-micro \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --no-service-account --no-scopes

gcloud compute scp linux-http-client client-vm:~ --zone=us-central1-a
gcloud compute ssh client-vm --zone=us-central1-a
chmod +x linux-http-client

./linux-http-client --domain 10.128.0.4 --port 8080 --num_requests 10 -i 9999 --bucket none --webdir none --verbose

gcloud compute instances delete client-vm --zone=us-central1-a --quiet
```


# Commands for the VM Running my Error Logging 

```
gcloud compute instances create error-logger-vm \
    --zone=us-central1-a \
    --machine-type=e2-micro \
    --service-account=web-server-sa@project-8aeecca0-4f70-4c6b-8e3.iam.gserviceaccount.com \
    --scopes=cloud-platform \
    --metadata-from-file=startup-script=error-logger-startup.sh,error-logger-py=error-logger/error-logger.py


gcloud compute instances add-metadata error-logger-vm \
    --zone=us-central1-a \
    --metadata-from-file=startup-script=error-logger-startup.sh,error-logger-py=error-logger/error-logger.py


gcloud compute instances reset error-logger-vm --zone=us-central1-a
```


# Database Commands 

Creating my database 
```
gcloud sql instances create my-class-db \
  --database-version=MYSQL_8_4 \
  --edition=ENTERPRISE \
  --tier=db-custom-4-16384 \
  --zone=us-east1-b \
  --availability-type=ZONAL \
  --storage-type=SSD \
  --storage-size=100GB \
  --root-password='YOUR_STRONG_PASSWORD' \
  --assign-ip \
  --authorized-networks=0.0.0.0/0 \
  --no-deletion-protection \
  --no-retain-backups-on-delete \
  --no-final-backup
```


Connecting to the DB locally 
```
gcloud sql connect my-class-db --user=root --project=project-8aeecca0-4f70-4c6b-8e3

USE hw5;
SHOW TABLES;
DESCRIBE requests;


gcloud sql instances patch my-class-db --activation-policy=ALWAYS --project=project-8aeecca0-4f70-4c6b-8e3
gcloud scheduler jobs pause stop-idle-db-hourly --location=us-east1 --project=project-8aeecca0-4f70-4c6b-8e3

```
