PROJECT_ID="project-8aeecca0-4f70-4c6b-8e3"
OLD_SA="homework4-service-account@${PROJECT_ID}.iam.gserviceaccount.com"
NEW_SA_NAME="homework5-service-account"
NEW_SA="${NEW_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. Create the new service account
gcloud iam service-accounts create "$NEW_SA_NAME" \
  --project="$PROJECT_ID" \
  --display-name="Homework 5 service account"

# 2. List the old account's project-level roles (check this output first)
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${OLD_SA}" \
  --format="value(bindings.role)"

# 3. Copy each of those roles to the new account
for ROLE in $(gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${OLD_SA}" \
    --format="value(bindings.role)"); do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${NEW_SA}" \
    --role="$ROLE" \
    --condition=None --quiet
done

# 4. Add the role for connecting to Cloud SQL
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${NEW_SA}" \
  --role="roles/cloudsql.client" \
  --condition=None --quiet

# 5. Confirm the result
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${NEW_SA}" \
  --format="table(bindings.role)"