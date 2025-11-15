# USF Moving Company Chatbot - Docker Deployment

This directory contains all the necessary files to deploy the USF Moving Company Chatbot using Docker.

## 📋 Analysis of app.py

### Application Architecture
- **Framework**: Flask with SocketIO support for real-time communication
- **Python Version**: 3.11 (recommended)
- **Server**: Gunicorn with eventlet worker for production deployment

### Key Dependencies
1. **AI & Communication**
   - OpenAI GPT models for chat and speech transcription
   - Twilio for voice call handling
   
2. **Data Storage & APIs**
   - Google Sheets for booking storage
   - Google Maps API for distance calculations
   - SMTP for email notifications

3. **Web Framework**
   - Flask for REST API endpoints
   - Flask-CORS for cross-origin requests (WordPress integration)
   - Flask-SocketIO for real-time speech streaming

### Environment Variables Required

#### Critical (Application won't work without these):
```
OPENAI_API_KEY          # OpenAI API access
TWILIO_ACCOUNT_SID      # Twilio account identifier
TWILIO_AUTH_TOKEN       # Twilio authentication
GOOGLE_MAPS_API_KEY     # Distance calculation
GOOGLE_SHEETS_CREDS     # JSON credentials for Google Sheets
BOOKING_SHEET_ID        # Google Sheets spreadsheet ID
EMAIL_ADDRESS           # SMTP email sender
EMAIL_PASSWORD          # SMTP password/app password
SMTP_SERVER             # SMTP server address
SMTP_PORT               # SMTP port (usually 587)
MANAGER_EMAIL           # Recipient for booking notifications
```

#### Optional (Has defaults):
```
PORT=5000                                              # Server port
FLASK_DEBUG=False                                      # Debug mode
OPENAI_MODEL=gpt-4o-mini                              # AI model
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe        # Transcription model
OFFICE_ADDRESS=2800 Rolido Dr Apt 238, Houston, TX... # Company address
DAILY_CAPACITY=3                                       # Max bookings/day
PEAK_DATES=2025-11-28,2025-12-25                      # Holiday pricing
SEND_CUSTOMER_EMAIL=False                              # Customer emails
TMP_DIR=/app/tmp                                       # Temp file storage
COMPANY_PHONE=+12817434503                             # Company phone
```

### Application Features
1. **Chat Endpoints**
   - `/chat` - Main text chat interface
   - `/speech-chat` - Audio transcription and response
   - `/chat/speech` - Alias for speech endpoint

2. **Booking System**
   - `/generate-estimate` - Calculate move estimates
   - `/submit-booking` - Submit booking requests
   - `/calculate-distance` - Distance calculations

3. **Additional Features**
   - `/welcome` - Get welcome message
   - `/request-call` - Request manager callback
   - `/twilio/voice` - Handle Twilio voice calls
   - `/reset-conversation` - Clear session history

4. **Real-time Features** (if SocketIO available)
   - Audio streaming with chunked upload
   - Live transcription
   - Real-time responses

## 🚀 Quick Start

### Option 1: PowerShell Script (Easiest)
```powershell
# Start the application
.\deploy.ps1 start

# View logs
.\deploy.ps1 logs

# Stop the application
.\deploy.ps1 stop

# See all options
.\deploy.ps1 -Action status
```

### Option 2: Docker Compose
```bash
# Copy and configure environment
cp .env.example .env
# Edit .env with your credentials

# Build and start
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Option 3: Docker CLI
```bash
# Build image
docker build -t usf-chatbot .

# Run container
docker run -d -p 5000:5000 --env-file .env --name usf-moving-chatbot usf-chatbot

# View logs
docker logs -f usf-moving-chatbot
```

## 📁 Files Created

1. **Dockerfile** - Container definition with all dependencies
2. **docker-compose.yml** - Multi-container orchestration (if needed)
3. **.env.example** - Template for environment variables
4. **.dockerignore** - Files to exclude from Docker build
5. **deploy.ps1** - PowerShell deployment script
6. **README.Docker.md** - Detailed deployment guide
7. **README.md** - This file

## 🔧 Configuration Steps

1. **Copy environment template**
   ```bash
   cp .env.example .env
   ```

2. **Edit .env file** with your credentials:
   - Get OpenAI API key from https://platform.openai.com/
   - Get Twilio credentials from https://console.twilio.com/
   - Setup Google Cloud project for Maps & Sheets API
   - Configure SMTP email (Gmail App Password recommended)

3. **Google Sheets Setup**
   - Create a Google Cloud service account
   - Enable Google Sheets API
   - Share your booking spreadsheet with service account email
   - Copy the entire JSON credentials into `GOOGLE_SHEETS_CREDS`

4. **Build and run**
   ```bash
   docker-compose up -d
   ```

## 🧪 Testing

Test the application is running:
```bash
# Health check
curl http://localhost:5000/

# Expected response:
# {"status":"online","service":"USF Moving Company Chatbot API","version":"1.0"}

# Get welcome message
curl http://localhost:5000/welcome

# Test chat endpoint
curl -X POST http://localhost:5000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello","session_id":"test123"}'
```

## 📊 Monitoring

### View Logs
```bash
docker-compose logs -f chatbot
```

### Check Container Status
```bash
docker-compose ps
```

### Health Check
The container includes automatic health checks every 30 seconds:
```bash
docker inspect usf-moving-chatbot | grep -A 10 Health
```

## 🔒 Security Notes

**Important**: 
- Never commit `.env` file to version control
- Use Docker secrets in production
- Rotate API keys regularly
- Enable HTTPS for production deployment
- Consider using a reverse proxy (nginx) for SSL termination

## 🐛 Troubleshooting

### Container won't start
```bash
# Check logs
docker-compose logs

# Verify environment variables
docker-compose config
```

### Port already in use
```bash
# Change PORT in .env file
PORT=8000

# Or stop conflicting service
netstat -ano | findstr :5000
```

### API errors
- Verify all API keys are valid and have proper permissions
- Check network connectivity from container
- Review application logs for specific error messages

## 📚 Additional Resources

- [Flask Documentation](https://flask.palletsprojects.com/)
- [Docker Documentation](https://docs.docker.com/)
- [OpenAI API Reference](https://platform.openai.com/docs/)
- [Twilio Documentation](https://www.twilio.com/docs)
- [Google Sheets API](https://developers.google.com/sheets/api)

## 💡 Next Steps

1. Configure all environment variables
2. Test locally with Docker
3. Set up CI/CD pipeline
4. Deploy to production (AWS, GCP, Azure, etc.)
5. Configure domain and SSL certificate
6. Set up monitoring and alerting
7. Implement backup strategy for Google Sheets data

## 🤝 Support

For issues or questions:
- Check the detailed guide: `README.Docker.md`
- Review application logs
- Verify all API credentials are correct
- Ensure all required services are accessible

---

**Created**: November 15, 2025  
**Docker Version**: 20.10+  
**Python Version**: 3.11  
**Application**: USF Moving Company Chatbot Backend
