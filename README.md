# Travel Content Bot

A production-ready Telegram bot for automated travel content creation using AI and image APIs.

## Features

- 🤖 **AI-Powered Content Generation**: Uses Google Gemini AI to generate comprehensive travel content
- 🖼️ **Image Integration**: Fetches high-quality images from Pexels API
- 📊 **MongoDB Storage**: Robust database storage with connection pooling
- 🔐 **Security Features**: Rate limiting, input sanitization, and user authorization
- 📱 **Telegram Integration**: Full Telegram bot API integration with inline keyboards
- ⏰ **Scheduling**: Schedule content posting for later publication
- 👥 **Multi-User Support**: Owner can manage authorized users

## Setup

### Prerequisites

- Python 3.8+
- MongoDB database
- Telegram Bot Token
- Google Gemini API Key
- Pexels API Key

### Installation

1. Clone the repository:

```bash
git clone https://github.com/pawansuthar01/travel-content-bot.git
cd travel-content-bot
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` file:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
GEMINI_API_KEY=your_gemini_api_key
PEXELS_API_KEY=your_pexels_api_key
MONGO_URI=your_mongodb_connection_string
TARGET_CHAT_ID=your_target_chat_id
GROUP_CHAT_ID=your_group_chat_id
OWNER_ID=your_telegram_user_id
```

4. Run the bot:

```bash
python bot.py
```

## Environment Variables

| Variable         | Required | Description                                     |
| ---------------- | -------- | ----------------------------------------------- |
| `TELEGRAM_TOKEN` | Yes      | Telegram bot token from @BotFather              |
| `GEMINI_API_KEY` | No       | Google Gemini API key for AI content generation |
| `PEXELS_API_KEY` | No       | Pexels API key for image fetching               |
| `MONGO_URI`      | Yes      | MongoDB connection string                       |
| `TARGET_CHAT_ID` | No       | Target chat/channel for posting                 |
| `GROUP_CHAT_ID`  | No       | Group chat for notifications                    |
| `OWNER_ID`       | Yes      | Telegram user ID of the bot owner               |

## Usage

### Commands

- `/start` - Start creating travel content
- `/end` - End current session
- `/help` - Show help information
- `/destinations` - List all posted destinations
- `/posted` - Show posting statistics
- `/adduser <user_id>` - Add authorized user (owner only)
- `/removeuser <user_id>` - Remove authorized user (owner only)
- `/listusers` - List authorized users (owner only)

### Content Creation Flow

1. Send `/start` to begin
2. Choose from AI-generated destination suggestions or type a custom destination
3. Confirm/reject thumbnail image
4. Confirm/reject gallery images
5. Review and confirm each content section (description, long description, tags, etc.)
6. Choose to post immediately, save as draft, or schedule for later

## Security Features

- **Rate Limiting**: 10 requests per minute per user
- **Input Sanitization**: Prevents injection attacks and malicious input
- **User Authorization**: Only authorized users can access content creation
- **API Key Protection**: Keys stored securely in environment variables

## Architecture

### Components

- **Telegram Bot**: Handles user interactions and commands
- **AI Service**: Google Gemini for content generation
- **Image Service**: Pexels API for image fetching
- **Database**: MongoDB for data persistence
- **Scheduler**: Built-in job queue for scheduled posting

### Database Schema

```javascript
{
  name: String,
  slug: String,
  thumbnail: { url: String, alt: String },
  images: [{ public_id: String, secure_url: String }],
  description: String,
  longDescription: String,
  category: String,
  bestTimeToVisit: String,
  tags: [String],
  popularFor: [String],
  SuggestedDuration: String,
  location: { country: String, region: String, coordinates: { latitude: Number, longitude: Number } },
  travelTips: [String],
  itinerary: [{ day: Number, title: String, activities: [String] }],
  weatherInfo: { avgTemp: String, climateType: String, bestMonth: String },
  isPublished: Boolean,
  createdBy: String,
  createdAt: Date,
  updatedAt: Date,
  scheduled_at: Date, // optional
  status: String // "published", "draft", "scheduled"
}
```

## Error Handling

The bot includes comprehensive error handling for:

- Network failures and API timeouts
- Database connection issues
- Invalid user input
- API rate limits
- Telegram API errors

## Logging

Logs are written to both console and `bot.log` file with the following levels:

- INFO: General operations
- WARNING: Non-critical issues
- ERROR: Critical errors requiring attention

## Deployment

### Production Deployment

1. Set up environment variables securely
2. Use a process manager like systemd or supervisor
3. Configure log rotation
4. Set up monitoring and alerts
5. Use a reverse proxy if needed

### Docker Deployment

```dockerfile
FROM python:3.9-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "bot.py"]
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

📧 **Email:** [mail@pawansuthar.in](mailto:mail@pawansuthar.in)  
💼 **LinkedIn:** [linkedin.com/in/pawankumar10/](https://www.linkedin.com/m/in/pawansuthar01/)
