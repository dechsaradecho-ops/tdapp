import sys
sys.path.insert(0, '..')
from app.core.config import get_settings

settings = get_settings()

print('=== Database Configuration ===')
print(f'SUPABASE_URL: {settings.supabase_url}')
print(f'SUPABASE_PUBLISHABLE_KEY: {settings.supabase_publishable_key[:20]}...' if settings.supabase_publishable_key else 'Not set')
print(f'APP_ENV: {settings.app_env}')
print()
print('Using effective service key:', settings.effective_service_key[:20] + '...' if settings.effective_service_key else 'Not set')
