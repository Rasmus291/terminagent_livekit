import requests
API_KEY = 'REMOVED_SECRET'

r = requests.get('https://api.elevenlabs.io/v1/shared-voices', params={
    'search': 'raho', 'page_size': '5',
}, headers={'xi-api-key': API_KEY})
voices = r.json().get('voices', [])
for v in voices:
    if v['voice_id'] == 'hLvRzHEBXR9scnhmrX9E':
        owner = v.get('public_owner_id', '')
        r2 = requests.post(
            f'https://api.elevenlabs.io/v1/voices/add/{owner}/{v["voice_id"]}',
            headers={'xi-api-key': API_KEY, 'Content-Type': 'application/json'},
            json={'new_name': 'Riya Rao'}
        )
        print(f"Add: {r2.status_code} {r2.text[:200]}")
        break
