// ─────────────────────────────────────────────
// S3 UPLOAD: Send successful CSV to S3 for PostIQ bot
// ─────────────────────────────────────────────
// SETUP:
//   1. Create an IAM user in AWS with ONLY this policy:
//      {
//        "Version": "2012-10-17",
//        "Statement": [{
//          "Effect": "Allow",
//          "Action": "s3:PutObject",
//          "Resource": "arn:aws:s3:::YOUR_BUCKET/uploads/*"
//        }]
//      }
//   2. In Apps Script, go to Project Settings > Script Properties
//   3. Add these properties:
//      AWS_ACCESS_KEY_ID     = (from IAM user)
//      AWS_SECRET_ACCESS_KEY = (from IAM user)
//      S3_BUCKET             = postiq-data-YOURACCOUNTID
//      S3_REGION             = us-east-1
// ────────────���────────────────────────────────


/**
 * Upload a blob to the S3 uploads/ folder.
 * Called from sendSpecificDrafts() after Drive save succeeds.
 */
function uploadToS3(blob) {
  var props     = PropertiesService.getScriptProperties();
  var accessKey = props.getProperty("AWS_ACCESS_KEY_ID");
  var secretKey = props.getProperty("AWS_SECRET_ACCESS_KEY");
  var bucket    = props.getProperty("S3_BUCKET");
  var region    = props.getProperty("S3_REGION") || "us-east-1";

  if (!accessKey || !secretKey || !bucket) {
    throw new Error("S3 credentials not configured in Script Properties");
  }

  var key         = "uploads/" + blob.getName();
  var content     = blob.getBytes();
  var contentType = "text/csv";

  var now       = new Date();
  var dateStamp = Utilities.formatDate(now, "UTC", "yyyyMMdd");
  var amzDate   = Utilities.formatDate(now, "UTC", "yyyyMMdd'T'HHmmss'Z'");
  var host      = bucket + ".s3." + region + ".amazonaws.com";

  // SHA-256 hash of the payload
  var payloadHash = sha256Hex(content);

  // Canonical headers
  var canonicalHeaders =
    "content-type:" + contentType + "\n" +
    "host:" + host + "\n" +
    "x-amz-content-sha256:" + payloadHash + "\n" +
    "x-amz-date:" + amzDate + "\n";

  var signedHeaders = "content-type;host;x-amz-content-sha256;x-amz-date";

  // Canonical request
  var canonicalUri     = "/" + key.split("/").map(encodeURIComponent).join("/");
  var canonicalRequest =
    "PUT\n" +
    canonicalUri + "\n" +
    "\n" +                    // empty query string
    canonicalHeaders + "\n" +
    signedHeaders + "\n" +
    payloadHash;

  // String to sign
  var credentialScope = dateStamp + "/" + region + "/s3/aws4_request";
  var stringToSign =
    "AWS4-HMAC-SHA256\n" +
    amzDate + "\n" +
    credentialScope + "\n" +
    sha256Hex(Utilities.newBlob(canonicalRequest).getBytes());

  // Signing key
  var signingKey = getSignatureKey(secretKey, dateStamp, region, "s3");

  // Signature
  var signature = hmacSha256Hex(signingKey, stringToSign);

  // Authorization header
  var authorization =
    "AWS4-HMAC-SHA256 " +
    "Credential=" + accessKey + "/" + credentialScope + ", " +
    "SignedHeaders=" + signedHeaders + ", " +
    "Signature=" + signature;

  // Make the request
  var url = "https://" + host + canonicalUri;

  var response = UrlFetchApp.fetch(url, {
    method:                  "PUT",
    contentType:             contentType,
    payload:                 content,
    muteHttpExceptions:      true,
    headers: {
      "Authorization":          authorization,
      "x-amz-date":             amzDate,
      "x-amz-content-sha256":   payloadHash
    }
  });

  var code = response.getResponseCode();
  if (code !== 200) {
    throw new Error("S3 upload failed (HTTP " + code + "): " + response.getContentText());
  }

  Logger.log("S3 upload success: " + key);
}


// ─────────────────────────────────────────────
// AWS Signature V4 helpers
// ──────��────────────────────────��─────────────

function sha256Hex(data) {
  var hash = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, data);
  return hash.map(function(b) {
    return ("0" + (b & 0xFF).toString(16)).slice(-2);
  }).join("");
}

function hmacSha256(key, message) {
  if (typeof message === "string") {
    message = Utilities.newBlob(message).getBytes();
  }
  return Utilities.computeHmacSha256Signature(message, key);
}

function hmacSha256Hex(key, message) {
  var sig = hmacSha256(key, message);
  return sig.map(function(b) {
    return ("0" + (b & 0xFF).toString(16)).slice(-2);
  }).join("");
}

function getSignatureKey(secretKey, dateStamp, region, service) {
  var kDate    = hmacSha256(Utilities.newBlob("AWS4" + secretKey).getBytes(), dateStamp);
  var kRegion  = hmacSha256(kDate, region);
  var kService = hmacSha256(kRegion, service);
  var kSigning = hmacSha256(kService, "aws4_request");
  return kSigning;
}
