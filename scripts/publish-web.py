"""Publish the static build for an existing File Arrival Watch stack."""
import argparse
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--stack", default="file-arrival-watch")
    parser.add_argument("--region", default="ap-south-1")
    args = parser.parse_args()

    def aws(*command, capture=False):
        result = subprocess.run(
            ["aws", "--no-cli-pager", "--profile", args.profile, "--region", args.region, *command],
            check=True, capture_output=capture, text=True,
        )
        return result.stdout

    subprocess.run(["npm", "run", "build"], cwd=ROOT, check=True)
    stack_json = aws("cloudformation", "describe-stacks", "--stack-name", args.stack, "--output", "json", capture=True)
    outputs = {item["OutputKey"]: item["OutputValue"] for item in json.loads(stack_json)["Stacks"][0]["Outputs"]}
    destination = "s3://" + outputs["SiteBucketName"]
    aws("s3", "sync", str(ROOT / "dist"), destination, "--exclude", "config.json", "--cache-control", "no-cache")
    config = {
        "apiUrl": outputs["ApiUrl"],
        "authUrl": outputs["AuthUrl"],
        "clientId": outputs["OperatorClientId"],
        "redirectUri": outputs["SiteUrl"],
    }
    with TemporaryDirectory() as directory:
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        aws("s3", "cp", str(path), destination + "/config.json",
            "--content-type", "application/json", "--cache-control", "no-store")
    print("Dashboard:", outputs["SiteUrl"])


if __name__ == "__main__":
    main()
