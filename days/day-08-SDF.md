
Today I learned the fundamentals of System Design by designing a simplified URL Shortener similar to Bitly. I explored how requests flow through a system, how databases store mappings, and how scalability can be improved using caching and load balancing.

Problem Statement

Build a URL Shortener that:

Accepts a long URL
Generates a short URL
Redirects users to the original URL
Supports millions of requests

Example:

Plain Text
1
Input:
2
https://www.example.com/blog/system-design-fundamentals
3
 
4
Output:
5
https://short.ly/a1b2c3
Show more lines
Functional Requirements
Users Should Be Able To
Create short links
Access original links through short URLs
Generate unique identifiers
High-Level Architecture
Plain Text
1
┌─────────────┐
2
│ User │
3
└──────┬──────┘
4
│
5
▼
6
┌─────────────────┐
7
│ Load Balancer │
8
└────────┬────────┘
9
│
10
┌──────────────┴──────────────┐
11
▼ ▼
12
┌─────────────────┐ ┌─────────────────┐
13
│ App Server A │ │ App Server B │
14
└────────┬────────┘ └────────┬────────┘
15
│ │
16
└──────────┬────────────────┘
17
▼
18
┌──────────────────┐
19
│ Redis Cache │
20
└────────┬─────────┘
21
│
22
▼
23
┌──────────────────┐
24
│ PostgreSQL DB │
25
└──────────────────┘
Show more lines
Request Flow
Creating a Short URL
Plain Text
1
User
2
↓
3
API
4
↓
5
Generate Short Code
6
↓
7
Save to Database
8
↓
9
Return Short URL
Show more lines
Redirecting
<img width="890" height="599" alt="image" src="https://github.com/user-attachments/assets/a02e9dc4-44cd-4562-ab84-e1201528aef2" />

Plain Text
1
User clicks short URL
2
↓
3
Check Redis Cache
4
↓
5
Cache Hit → Redirect
6
↓
7
Cache Miss
8
↓
9
Database Lookup
10
↓
11
Store in Cache
12
↓
13
Redirect User
Show more lines
Database Design
SQL
1
CREATE TABLE urls (
2
id SERIAL PRIMARY KEY,
3
short_code VARCHAR(10) UNIQUE NOT NULL,
4
original_url TEXT NOT NULL,
5
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
6
);
Show more lines
Low-Level Design
URL Entity
Python
1
class URL:
2
def __init__(self, short_code, original_url):
3
self.short_code = short_code
4
self.original_url = original_url
Show more lines
Working Python Demonstration

This example mimics the behavior of a URL shortener.

Python
1
import random
2
import string
3
 
4
class URLShortener:
5
 
6
def __init__(self):
7
self.database = {}
8
 
9
def generate_code(self, length=6):
10
chars = string.ascii_letters + string.digits
11
return ''.join(random.choice(chars) for _ in range(length))
12
 
13
def shorten(self, original_url):
14
 
15
code = self.generate_code()
16
 
17
while code in self.database:
18
code = self.generate_code()
19
 
20
self.database[code] = original_url
21
 
22
return f"https://short.ly/{code}"
23
 
24
def redirect(self, short_url):
25
 
26
code = short_url.split("/")[-1]
27
 
28
if code in self.database:
29
return self.database[code]
30
 
31
return "URL Not Found"
32
 
33
 
34
# Demo
35
 
36
service = URLShortener()
37
 
38
short_url = service.shorten(
39
"https://www.example.com/system-design"
40
)
41
 
42
print("Generated:", short_url)
43
 
44
print(
45
"Redirects To:",
46
service.redirect(short_url)
47
)
Show more lines
Example Output
Plain Text
1
Generated: https://short.ly/A4gT91
2
 
3
Redirects To:
4
https://www.example.com/system-design
Show more lines
Scaling Challenges

Imagine:

Plain Text
1
100 Users
2
↓
3
1,000 Users
4
↓
5
100,000 Users
6
↓
7
10 Million Users
Show more lines

A single server will eventually fail to keep up.

Solution 1: Load Balancing

Instead of:

Plain Text
1
Users
2
↓
3
Server
Show more lines

Use:

Plain Text
1
Users
2
↓
3
Load Balancer
4
↓
5
Server 1
6
Server 2
7
Server 3
Show more lines

Benefits:

Fault tolerance
Better performance
Horizontal scaling
Solution 2: Redis Cache

Without Cache:

Plain Text
1
Request
2
↓
3
Database
4
``
Show more lines

With Cache:

Plain Text
1
Request
2
↓
3
Redis
4
↓
5
Database (only if needed)
6
 
Show more lines

Popular URLs stay in memory.

Result:

Faster lookup
Lower DB load
Better user experience
Solution 3: Database Replication
Plain Text
1
Master DB
2
│
3
┌───────────┴───────────┐
4
▼ ▼
5
Read Replica 1 Read Replica 2
Show more lines

Writes go to master.

Reads go to replicas.

Benefits:

Increased read capacity
Better reliability
System Design Concepts Learned
Scalability

Ability to handle growth.

Plain Text
1
1K Users
2
10K Users
3
100K Users
4
1M Users
Show more lines

without major redesign.

Availability

System remains accessible.

Example:

Plain Text
1
99.99% uptime
Show more lines
Reliability

System performs correctly even during failures.

Latency

Time required to return a response.

Plain Text
1
50ms
2
100ms
3
200ms
Show more lines

Smaller is better.

Interview Perspective

Common questions inspired by this design:

How do you prevent duplicate short URLs?

Use:

Python
1
while code in database:
2
generate_new_code()
Show more lines
How do you handle billions of URLs?

Use:

Sharding
Distributed databases
Caching
Multiple application servers
What happens if Redis crashes?

Fallback:

Plain Text
1
Redis Failure
2
↓
3
Database Lookup
4
↓
5
System Continues Working
Show more lines
One Thing I Learned

System Design is not about writing more code. It is about designing systems that continue to work efficiently when the number of users grows from hundreds to millions.
