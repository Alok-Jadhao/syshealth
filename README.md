# 🚀 SysHealth: Cloud-Based Linux System Health Monitoring

![Python](https://img.shields.io/badge/Python-3.x-blue)
![Platform](https://img.shields.io/badge/Platform-Linux-green)
![Status](https://img.shields.io/badge/Status-Active-success)

---

## 📑 Table of Contents

* [Description](#-description)
* [Features](#-features)
* [Tech Stack](#-tech-stack)
* [Installation](#-installation)
* [Running Locally](#-running-locally)
* [Running on Cloud (EC2)](#-running-on-cloud-ec2)
* [Project Structure](#-project-structure)
* [Configuration](#-configuration)
* [API Reference](#-api-reference)

---

## 📌 Description

SysHealth is a Linux system monitoring tool that detects **real performance degradation** using kernel-level metrics like **Pressure Stall Information (PSI)**.

Unlike traditional tools such as `top` or `htop`, which only show usage percentages, SysHealth detects **resource contention** — when processes are waiting for CPU, memory, or I/O.

It also supports **cloud-based monitoring**, where system data is sent to a remote server for centralized monitoring.

---

## ✨ Features

* PSI-based monitoring (real performance insight)
* Adaptive baseline (no fixed thresholds)
* Health classification:

  * HEALTHY
  * DEGRADED
  * CRITICAL
* Moving average + persistence logic
* Cloud monitoring via Flask + EC2
* REST API integration
* Lightweight and efficient

---

## 🛠️ Tech Stack

* Python 3
* Linux (`/proc` filesystem)
* Flask (for cloud server)
* AWS EC2
* Requests library

---

## ⚙️ Installation

### 1. Clone the repository

```bash
git clone https://github.com/Alok-Jadhao/syshealth.git
cd syshealth
```

### 2. Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install flask requests
```

> Optional (recommended): create a `requirements.txt`

```bash
pip freeze > requirements.txt
```

---

## ▶️ Running Locally

Run the main monitoring script:

```bash
python3 main.py
```

If your project uses a different entry point, locate it using:

```bash
ls
```

---

## ☁️ Running on Cloud (EC2)

1. Launch an EC2 instance (Ubuntu recommended)
2. SSH into the instance:

```bash
ssh ubuntu@your-ec2-ip
```

3. Install dependencies:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git -y
```

4. Clone and setup:

```bash
git clone https://github.com/Alok-Jadhao/syshealth.git
cd syshealth
python3 -m venv .venv
source .venv/bin/activate
pip install flask requests
```

5. Run the server:

```bash
python3 main.py
```

---

## 📁 Project Structure

```
syshealth/
│── main.py
│── monitor/
│── api/
│── utils/
│── README.md
```

> Structure may vary depending on implementation.

---

## ⚙️ Configuration

* Uses Linux `/proc` filesystem for metrics
* Ensure your system supports PSI:

```bash
cat /proc/pressure/cpu
```

If this file is missing, PSI is not supported on your kernel.

---

## 🔗 API Reference

Example endpoint:

```
GET /health
```

Response:

```json
{
  "status": "HEALTHY"
}
```

---


